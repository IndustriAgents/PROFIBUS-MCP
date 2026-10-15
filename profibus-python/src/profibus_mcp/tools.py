"""Compatibility imports; implementation lives in domain, application and protocol modules."""

import json as json
import logging as logging
from dataclasses import dataclass as dataclass
from dataclasses import field as field
from pathlib import Path as Path
from typing import Annotated as Annotated
from typing import Any as Any

from pydantic import Field as Field
from pydantic import Strict as Strict

from profibus_mcp import __version__ as __version__
from profibus_mcp.application.resources import ToolResources as ToolResources
from profibus_mcp.config import MAX_STATION_ADDRESS as MAX_STATION_ADDRESS
from profibus_mcp.config import ProfibusConfig as ProfibusConfig
from profibus_mcp.domain.errors import MAX_DP_DATA_LEN as MAX_DP_DATA_LEN
from profibus_mcp.domain.errors import ProfibusMasterError as ProfibusMasterError
from profibus_mcp.domain.errors import request_cancel_scope as request_cancel_scope
from profibus_mcp.domain.errors import validate_slave_address as validate_slave_address
from profibus_mcp.domain.model import NOT_SENT as NOT_SENT
from profibus_mcp.domain.model import WRITE_TOOLS as WRITE_TOOLS
from profibus_mcp.domain.model import Alias as Alias
from profibus_mcp.domain.model import ByteValue as ByteValue
from profibus_mcp.domain.model import Envelope as Envelope
from profibus_mcp.domain.model import OutputData as OutputData
from profibus_mcp.domain.model import ReadLength as ReadLength
from profibus_mcp.domain.model import SlaveAddress as SlaveAddress
from profibus_mcp.domain.model import alias_problem as alias_problem
from profibus_mcp.domain.model import envelope as envelope
from profibus_mcp.domain.model import err as err
from profibus_mcp.domain.model import log as log
from profibus_mcp.domain.model import ok as ok
from profibus_mcp.gsd_parser import GSDParser as GSDParser
from profibus_mcp.gsd_parser import GSDParserError as GSDParserError
from profibus_mcp.infrastructure.maps import SlaveMap as SlaveMap
from profibus_mcp.pb_master import ProfibusMaster as ProfibusMaster

from .protocol.support import _LOCAL as _LOCAL
from .protocol.support import _READ as _READ
from .protocol.support import _WRITE as _WRITE
from .protocol.support import FastMCP as FastMCP
from .protocol.support import ToolAnnotations as ToolAnnotations
from .protocol.support import capture_request_cancel_scope as capture_request_cancel_scope
from .protocol.tools import register_tools as register_tools
