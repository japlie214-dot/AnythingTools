# utils/browser_daemon.py
# NOTE: This is a personal project for a single user in Windows.
import os
import collections
import threading
import sys
from datetime import datetime, timezone
from enum import Enum

# Third-party imports
try:
    import psutil
except ImportError:
    psutil = None

from botasaurus.browser import Driver, cdp
import config
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)


# ── Health State Machine ─────────────────────────────────────────────────────
class BrowserStatus(Enum):
    INITIALIZING = "INITIALIZING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    CRITICAL_FAILURE = "CRITICAL_FAILURE"


# ── ChromeDaemonManager Infrastructure ───────────────────────────────────────
class ChromeDaemonManager:
    """
    Centralized browser lifecycle manager with surgical process management,
    deep-stack warmup verification, and orchestrated shutdown.
    """
    
    def __init__(self):
        self._driver: Driver | None = None
        self._id_tracking: dict = {}
        self._action_log: collections.deque = collections.deque(maxlen=50)
        self._lock = threading.Lock()
        self._status = BrowserStatus.INITIALIZING
        self._pid: int | None = None
    
    @property
    def status(self) -> BrowserStatus:
        """Get current browser health status."""
        return self._status
    
    @property
    def pid(self) -> int | None:
        """Get current Chrome process PID."""
        return self._pid
    
    def is_driver_alive(self) -> bool:
        """Lightweight health check."""
        if self._driver is None:
            return False
        try:
            self._driver.run_js("return 1;")
            return True
        except Exception:
            return False
    
    def surgical_kill(self) -> None:
        """
        Kill processes holding the specific profile lock via command-line inspection.
        Fails app immediately on permission errors.
        """
        if psutil is None:
            log.dual_log(tag="Browser:Kill", message="psutil not available, skipping surgical kill", level="WARNING")
            return
        
        target_dir = os.path.abspath(config.CHROME_USER_DATA_DIR).lower()
        killed = False
        
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            # Access pid immediately as it is always available and won't raise
            current_pid = proc.pid
            try:
                info = proc.info
                if not info:
                    continue
                cmdline = " ".join(info.get('cmdline') or []).lower()
                proc_name = (info.get('name') or "").lower()
                
                # Check if this is a Chrome process for our profile
                if "chrome" in proc_name and target_dir in cmdline:
                    proc.kill()
                    killed = True
                    log.dual_log(tag="Browser:Kill", message=f"Killed Chrome PID {current_pid} for profile {target_dir}")
            except (psutil.AccessDenied, psutil.PermissionError) as e:
                log.dual_log(tag="Browser:Kill", message=f"Permission denied killing Chrome PID {current_pid}: {e}", level="CRITICAL")
                sys.exit(1)
            except psutil.NoSuchProcess:
                continue
            except Exception as e:
                log.dual_log(tag="Browser:Kill", message=f"Error killing process: {e}", level="WARNING")
        
        if killed:
            log.dual_log(tag="Browser:Kill", message=f"Surgically killed Chrome processes for {target_dir}")
    
    def _init_driver(self) -> Driver:
        """
        Create and configure the Driver with Botasaurus-specific fixes.
        Logs the spawned Chrome PID.
        """
        self._status = BrowserStatus.INITIALIZING
        
        # Kill any existing Chrome processes for this profile
        self.surgical_kill()
        
        # Initialize driver with Botasaurus parameters
        # Note: Using 'profile' parameter instead of arguments for Botasaurus compatibility
        self._driver = Driver(
            headless=False,
            user_agent="REAL",
            window_size=(1920, 1080),
            profile=os.path.abspath(config.CHROME_USER_DATA_DIR),
        )
        
        # Audit and log the spawned Chrome PID
        try:
            if hasattr(self._driver, 'browser') and hasattr(self._driver.browser, 'process'):
                self._pid = self._driver.browser.process.pid
                log.dual_log(tag="Browser:Daemon", message=f"Chrome spawned with PID {self._pid}")
            else:
                log.dual_log(tag="Browser:Daemon", message="Chrome PID unavailable (driver structure unexpected)", level="WARNING")
        except Exception as e:
            log.dual_log(tag="Browser:Daemon", message=f"Failed to capture Chrome PID: {e}", level="WARNING")
            self._pid = None
        
        # Clear SoM tracking for new session
        self._id_tracking.clear()
        # Status remains INITIALIZING until deep_warmup completes successfully
        
        return self._driver
    
    def get_or_create_driver(self) -> Driver:
        """Return the live Driver instance, re-initializing if session is dead."""
        with self._lock:
            if self._driver is None or not self.is_driver_alive():
                log.dual_log(tag="Browser:Daemon", message="Initialising new Driver session.")
                if self._driver is not None:
                    try:
                        self._driver.close()
                    except Exception as e:
                        log.dual_log(tag="Browser:Daemon", message=f"Error closing old driver: {e}", level="WARNING")
                self._init_driver()
            return self._driver
    
    def shutdown_driver(self) -> None:
        """Gracefully close the Driver and mark status as CRITICAL_FAILURE."""
        with self._lock:
            if self._driver is not None:
                try:
                    self._driver.close()
                except Exception as e:
                    log.dual_log(tag="Browser:Daemon", message=f"Error closing driver: {e}", level="WARNING")
                self._driver = None
                self._status = BrowserStatus.CRITICAL_FAILURE
                log.dual_log(tag="Browser:Daemon", message="Driver shut down.")
    
    async def deep_warmup(self) -> bool:
        """
        Full stack verification: Navigation -> SoM -> Vision.
        Returns True if successful, False otherwise.
        """
        try:
            from utils.browser_utils import safe_google_get
            from utils.som_utils import inject_som
            from utils.vision_utils import capture_and_optimize
            
            driver = self.get_or_create_driver()
            
            # Phase 1: Navigation Test
            log.dual_log(tag="Startup:Warmup", message="Phase 1: Navigation Test")
            safe_google_get(driver, "https://example.com")
            driver.sleep(2)
            if "Example Domain" not in (driver.page_html or ""):
                raise RuntimeError("Navigation failed: Content mismatch")
            
            # Phase 2: SoM Test
            log.dual_log(tag="Startup:Warmup", message="Phase 2: SoM Injection Test")
            last_id = inject_som(driver)
            if last_id <= 1:
                raise RuntimeError("SoM Injection failed: No markers added")
            
            # Phase 3: Vision Test
            log.dual_log(tag="Startup:Warmup", message="Phase 3: Vision Subsystem Test")
            slices = capture_and_optimize(driver, 0)
            if not slices or not any(s.get("b64") for s in slices if s.get("status") == "OK"):
                raise RuntimeError("Vision test failed: No valid slices produced")
            
            log.dual_log(tag="Startup:Warmup", message="Deep Warmup Successful")
            # Set READY only after full verification
            self._status = BrowserStatus.READY
            return True
        except Exception as e:
            log.dual_log(tag="Startup:Warmup", message=f"CRITICAL: Warmup Failed: {e}", level="CRITICAL")
            self._status = BrowserStatus.CRITICAL_FAILURE
            return False
    
    def get_id_tracking(self) -> dict:
        """Return the shared SoM id-tracking dict by reference."""
        return self._id_tracking
    
    def append_action_log(self, entry: dict) -> None:
        """Append a sanitised entry to the rolling log."""
        safe = {
            "tool_name": str(entry.get("tool_name", ""))[:200],
            "args_summary": {str(k): str(v)[:200] for k, v in entry.get("args_summary", {}).items()},
            "outcome": str(entry.get("outcome", ""))[:200],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._action_log.append(safe)
    
    def get_action_log_snapshot(self, count: int) -> list[dict]:
        """Thread-safe snapshot of the last *count* entries."""
        return list(self._action_log)[-max(1, min(count, 50)):]


# ── Singleton Instance ───────────────────────────────────────────────────────
daemon_manager = ChromeDaemonManager()


# ── Legacy Accessor Functions (Backward Compatibility) ───────────────────────
def get_or_create_driver() -> Driver:
    """Legacy accessor - uses daemon_manager instance."""
    return daemon_manager.get_or_create_driver()


def is_driver_alive() -> bool:
    """Legacy accessor - uses daemon_manager instance."""
    return daemon_manager.is_driver_alive()


def shutdown_driver() -> None:
    """Legacy accessor - uses daemon_manager instance."""
    daemon_manager.shutdown_driver()


def get_id_tracking() -> dict:
    """Legacy accessor - uses daemon_manager instance."""
    return daemon_manager.get_id_tracking()


def append_action_log(entry: dict) -> None:
    """Legacy accessor - uses daemon_manager instance."""
    daemon_manager.append_action_log(entry)


def get_action_log_snapshot(count: int) -> list[dict]:
    """Legacy accessor - uses daemon_manager instance."""
    return daemon_manager.get_action_log_snapshot(count)
