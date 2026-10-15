from __future__ import annotations

from dataclasses import dataclass, field

from profibus_mcp.config import ProfibusConfig
from profibus_mcp.domain.ports import ProfibusMasterPort
from profibus_mcp.gsd_parser import GSDParser
from profibus_mcp.infrastructure.maps import SlaveMap


@dataclass(slots=True)
class ToolResources:
    config: ProfibusConfig
    master: ProfibusMasterPort
    slave_map: SlaveMap
    gsd_parser: GSDParser = field(default_factory=GSDParser)
