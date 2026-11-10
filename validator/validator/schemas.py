from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

State = Literal["included", "not_included", "unknown"]
Status = Literal["match", "partial", "contradiction", "missing", "extra", "unknown"]
CategoryName = Literal[
    "collateral_structure",
    "lien_relations",
    "financial_values",
    "property_details",
    "machine_details",
    "list_memberships",
]


class Address(BaseModel):
    province: str | None = None
    city_county: str | None = None
    town: str | None = None
    village: str | None = None
    lot_number: str | None = None


class SourceRef(BaseModel):
    sheet: str | None = None
    cell: str | None = None
    row: int | None = None
    column: int | None = None
    text: str | None = None


class CollateralGroup(BaseModel):
    group_id: str
    group_type: Literal["real_estate", "machine", "mixed", "unknown"] = "unknown"
    properties: list[str] = Field(default_factory=list)
    valuation: float | int | None = None
    description: str | None = None
    source_refs: list[SourceRef] = Field(default_factory=list)

    @field_validator("properties", mode="before")
    @classmethod
    def property_refs_must_be_ids(cls, value: Any) -> list[str]:
        if value is None:
            return []
        result: list[str] = []
        for item in value:
            if isinstance(item, str):
                result.append(item)
            elif isinstance(item, dict) and item.get("property_id"):
                result.append(str(item["property_id"]))
            else:
                raise ValueError("collateral_group.properties must contain property_id strings")
        return result


class Property(BaseModel):
    property_id: str
    property_type: Literal["land", "building", "unknown"] = "unknown"
    address: Address = Field(default_factory=Address)
    land_category: str | None = None
    area_m2: float | None = None
    building_structure: str | None = None
    usage: str | None = None
    description: str | None = None
    source_refs: list[SourceRef] = Field(default_factory=list)


class Lien(BaseModel):
    group_id: str
    rank: int | None = None
    registration_date: str | None = None
    creditor: str | None = None
    debtor: str | None = None
    maximum_claim: float | int | None = None
    joint_collateral: list[str] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(default_factory=list)


class Machine(BaseModel):
    machine_id: str
    serial_no: str | None = None
    name: str | None = None
    maker: str | None = None
    manufactured_date: str | None = None
    specification: str | None = None
    quantity: float | int | None = None
    unit_price: float | int | dict[str, Any] | None = None
    amount: float | int | dict[str, Any] | None = None
    source_refs: list[SourceRef] = Field(default_factory=list)


class ListMembership(BaseModel):
    machine_id: str
    list_no: str
    state: State = "unknown"
    source_refs: list[SourceRef] = Field(default_factory=list)


class ParsedBusinessTable(BaseModel):
    table_id: str
    table_type: Literal["real_estate_collateral", "machine_collateral", "machine_register"]
    title: str
    summary: str
    collateral_group_ids: list[str] = Field(default_factory=list)
    property_ids: list[str] = Field(default_factory=list)
    lien_keys: list[str] = Field(default_factory=list)
    machine_ids: list[str] = Field(default_factory=list)
    membership_count: int = 0


class DomainJSON(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    collateral_groups: list[CollateralGroup] = Field(default_factory=list)
    properties: list[Property] = Field(default_factory=list)
    liens: list[Lien] = Field(default_factory=list)
    machines: list[Machine] = Field(default_factory=list)
    list_memberships: list[ListMembership] = Field(default_factory=list)
    document_tables: list[ParsedBusinessTable] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class Weights(BaseModel):
    collateral_structure: float = 0.15
    lien_relations: float = 0.20
    financial_values: float = 0.20
    property_details: float = 0.15
    machine_details: float = 0.15
    list_memberships: float = 0.15

    @model_validator(mode="after")
    def sum_to_one(self) -> "Weights":
        total = sum(self.model_dump().values())
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"weights must sum to 1.0, got {total}")
        return self


class ValidatorConfig(BaseModel):
    weights: Weights = Field(default_factory=Weights)
    gt_blank_membership: Literal["not_included", "unknown"] = "not_included"
    llm_cache_path: str = ".omx/llm_judgement_cache.json"


class ObjectMatch(BaseModel):
    category: str
    gt_id: str
    target_id: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: dict[str, Any] = Field(default_factory=dict)


class ErrorItem(BaseModel):
    category: CategoryName
    object: str
    field: str
    gt: Any = None
    target: Any = None
    status: Status
    credit: float = Field(ge=0.0, le=1.0)
    reason: str
    root_cause_id: str | None = None


class CategoryScore(BaseModel):
    precision: float = Field(ge=0.0, le=1.0)
    recall: float = Field(ge=0.0, le=1.0)
    f1: float = Field(ge=0.0, le=1.0)
    matched_credit: float = Field(ge=0.0)
    target_total: float = Field(ge=0.0)
    gt_total: float = Field(ge=0.0)
    rationale: str


class CategoryEvaluation(CategoryScore):
    category: CategoryName


class StatusCounts(BaseModel):
    match: int = Field(default=0, ge=0)
    partial: int = Field(default=0, ge=0)
    contradiction: int = Field(default=0, ge=0)
    missing: int = Field(default=0, ge=0)
    extra: int = Field(default=0, ge=0)
    unknown: int = Field(default=0, ge=0)


class LLMValidationResult(BaseModel):
    category_evaluations: list[CategoryEvaluation]
    counts: StatusCounts
    errors: list[ErrorItem]
    object_matches: list[ObjectMatch]
    llm_judgements: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("category_evaluations")
    @classmethod
    def all_categories_present(cls, value: list[CategoryEvaluation]) -> list[CategoryEvaluation]:
        required = {
            "collateral_structure",
            "lien_relations",
            "financial_values",
            "property_details",
            "machine_details",
            "list_memberships",
        }
        present = {item.category for item in value}
        missing = required - present
        if missing:
            raise ValueError(f"missing category_evaluations: {sorted(missing)}")
        return value


class ValidationReport(BaseModel):
    score: float
    category_scores: dict[str, dict[str, float | str]]
    counts: dict[str, int]
    errors: list[ErrorItem]
    object_matches: list[ObjectMatch]
    derived_values: list[dict[str, Any]]
    llm_judgements: list[dict[str, Any]]
    metadata: dict[str, Any]
