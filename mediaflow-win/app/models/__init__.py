"""ORM 模型汇总导出 - 确保所有模型注册到 Base.metadata。"""

from app.models.directory_group import DirectoryGroup
from app.models.gap_fill import GapFillEpisode, GapFillSeasonRun
from app.models.gap_fill_site_rule import GapFillSiteRule, GapFillWorkPreference
from app.models.manual_confirmation import ManualSeasonConfirmation
from app.models.metadata_cache import MetadataCache
from app.models.operation import OperationLog
from app.models.season import SeasonRecord
from app.models.season_mapping import SeasonMapping
from app.models.task import Task
from app.models.transfer_repair import (
    TransferRepairBaseline,
    TransferRepairItem,
    TransferRepairRule,
)

__all__ = [
    "Task",
    "SeasonRecord",
    "OperationLog",
    "SeasonMapping",
    "ManualSeasonConfirmation",
    "MetadataCache",
    "GapFillEpisode",
    "GapFillSeasonRun",
    "DirectoryGroup",
    "GapFillSiteRule",
    "GapFillWorkPreference",
    "TransferRepairBaseline",
    "TransferRepairItem",
    "TransferRepairRule",
]
