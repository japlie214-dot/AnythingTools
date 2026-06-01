# database/backup/engine/base.py
from abc import ABC, abstractmethod

class BackupEngine(ABC):
    @abstractmethod
    def startup(self) -> dict:
        pass

    @abstractmethod
    def shutdown(self) -> None:
        pass
