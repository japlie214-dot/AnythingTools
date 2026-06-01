# database/backup/settings.py
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class LocalBackupSettings(BaseSettings):
    enabled: bool = True
    db_path: str = 'data/backup.db'
    allow_drop_tables: bool = True

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

    @model_validator(mode='after')
    def validate_at_least_one_mode(self):
        if not self.local.enabled and not self.cloud.enabled:
            raise ValueError('At least one backup mode must be enabled')
        if self.cloud.enabled and not self.cloud.account:
            raise ValueError('Cloud mode requires BACKUP_CLOUD__ACCOUNT')
        return self
