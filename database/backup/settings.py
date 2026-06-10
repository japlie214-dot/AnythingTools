# database/backup/settings.py
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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

from typing import Literal

class HITLConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix='BACKUP_SYNC_', extra='ignore')
    strategy: Literal["auto_recommend", "newest_overall_wins", "operational_wins", "cloud_wins", "abort"] = "auto_recommend"
    interactive: bool = False
    per_table: dict[str, str] = Field(default_factory=dict)
    auto_accept_on_no_conflict: bool = True

class Vec0BackupSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix='BACKUP_VEC0_', extra='ignore')
    enabled: bool = True
    use_native_vector_type: bool = True
    dim: int = 1024
    chunk_size: int = 1024
    push_batch_size: int = 256
    rehydrate_chunk_size: int = 1024

class BackupSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix='BACKUP_',
        env_nested_delimiter='__',
        env_file='.env',
        extra='ignore'
    )
    cloud: CloudBackupSettings = CloudBackupSettings()
    sync: SyncSettings = SyncSettings()
    hitl: HITLConfig = HITLConfig()
    vec0: Vec0BackupSettings = Vec0BackupSettings()

    @model_validator(mode='after')
    def validate_cloud_config(self):
        if self.cloud.enabled and not self.cloud.account:
            raise ValueError('Cloud mode requires BACKUP_CLOUD__ACCOUNT')
        return self
