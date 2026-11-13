from pydantic import BaseModel


class InspectRequest(BaseModel):
    dataset_id: int


class RuleEnablePatch(BaseModel):
    enabled: bool


class WaiverCreate(BaseModel):
    rule_id: int
    # 可选实体键：空串/None = 该规则下全部实体；点名后只豁免这一个键
    entity_key: str | None = None
    starts_at: str
    ends_at: str
    note: str | None = None
