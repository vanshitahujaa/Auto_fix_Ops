from pydantic import BaseModel, Field
from typing import Dict, Any, Optional

class AlertmanagerPayload(BaseModel):
    receiver: str
    status: str
    alerts: list[Dict[str, Any]]
    groupLabels: Dict[str, str] = Field(default_factory=dict)
    commonLabels: Dict[str, str] = Field(default_factory=dict)
    commonAnnotations: Dict[str, str] = Field(default_factory=dict)
    externalURL: str
    version: str
    groupKey: str
