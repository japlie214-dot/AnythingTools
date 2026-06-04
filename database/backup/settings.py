# database/backup/settings.py
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class LocalBackupSettings(BaseSettings):
    enabled: bool = True
    db_path: str = 'data/backup.db'
    allow_drop_tables: bool = True
    checkpoint_before_snapshot_seconds: int = Field(default=5, ge=0, le=60)
    vec0_integrity_check: bool = True

class CloudBackupSettings(BaseSettings):
    enabled: bool = False
    account: str = ''
    user: str = ''
    warehouse: str = ''
    database: str = ''
    schema_name: str = Field(default='BACKUP', alias='schema')
    private_key_path: str = 'snowflake_private_key.p8'
    pool_size: int = Field(default=5, ge=1, le=20)
    max_overflow: int = Field(default=10, ge=0, le=30)

class SyncSettings(BaseSettings):
    batch_size: int = Field(default=500, ge=50, le=5000)
    max_retries: int = Field(default=5, ge=1, le=10)
    circuit_breaker_threshold: int = Field(default=3, ge=1)
    circuit_breaker_reset_seconds: int = Field(default=300)

from enum import Enum

class StrategyMode(str, Enum):
    AUTO_RECOMMEND = "auto_recommend"
    THREE_WAY_MERGE = "three_way_merge"
    NEWEST_WINS = "newest_wins"
    LOCAL_BACKUP_WINS = "local_backup_wins"
    CLOUD_BACKUP_WINS = "cloud_backup_wins"
    OPERATIONAL_WINS = "operational_wins"
    ABORT = "abort"

class DecisionSource(str, Enum):
    ENV = "env"
    API = "api"
    TERMINAL = "terminal"
    AUTO_ACCEPT = "auto"

class HITLConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix='BACKUP_SYNC_', extra='ignore')
    strategy: StrategyMode = StrategyMode.AUTO_RECOMMEND
    interactive: bool = False
    per_table: dict[str, StrategyMode] = Field(default_factory=dict)
    auto_accept_on_no_conflict: bool = True

class Vec0BackupSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix='BACKUP_VEC0_', extra='ignore')
    enabled: bool = False
    dim: int = 1024
    chunk_size: int = 1024
    push_batch_size: int = 256
    rehydrate_chunk_size: int = 1024

class ContentHashConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix='BACKUP_HASH_', extra='ignore')
    enabled: bool = True
    exclude_columns: set[str] = Field(default_factory=lambda: {"embedding", "vec_rowid"})

class BackupSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix='BACKUP_',
        env_nested_delimiter='__',
        env_file='.env',
        extra='ignore'
    )
    local: LocalBackupSettings = LocalBackupSettings()
    cloud: CloudBackupSettings = CloudBackupSettings()
    sync: SyncSettings = SyncSettings()
    hitl: HITLConfig = HITLConfig()
    vec0: Vec0BackupSettings = Vec0BackupSettings()
    content_hash: ContentHashConfig = ContentHashConfig()

    @model_validator(mode='after')
    def validate_at_least_one_mode(self):
        if not self.local.enabled and not self.cloud.enabled:
            raise ValueError('At least one backup mode must be enabled')
        if self.cloud.enabled and not self.cloud.account:
            raise ValueError('Cloud mode requires BACKUP_CLOUD__ACCOUNT')
        return self
