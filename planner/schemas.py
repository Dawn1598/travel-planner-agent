from typing import Literal
from pydantic import BaseModel, Field, ConfigDict, StrictBool


class Analysis(BaseModel):
    title: str = '新行程'
    city: str = ''
    requirements: dict = Field(default_factory=dict)
    question: str = ''


class Activity(BaseModel):
    time: str = Field(description='开始时间 HH:MM')
    end_time: str = Field(description='结束时间 HH:MM，可用 24:00 表示当天结束；跨日活动分段记录')
    name: str
    kind: Literal['景点', '餐饮', '酒店', '交通', '休息'] = '景点'
    description: str = ''
    location: str = Field(default='', description='高德确认的经度,纬度；交通活动填终点坐标，未知留空')
    poi_id: str = ''
    travel_minutes: int = Field(default=0, ge=0, description='从上一项前往此处的交通分钟数；如果上一项已是到达此处的交通活动，填0')
    transport: str = ''
    cost: str = Field(default='', description='估计费用；未知留空，不能编造实时房价')
    source_ids: list[str] = Field(default_factory=list)


class Day(BaseModel):
    day: int = Field(ge=1)
    title: str
    date: str = ''
    activities: list[Activity] = Field(default_factory=list)


class PlanDetails(BaseModel):
    title: str
    city: str
    summary: str
    estimated_budget: str = ''
    notes: list[str] = Field(default_factory=list)
    map_url: str = ''


class Plan(PlanDetails):
    days: list[Day] = Field(min_length=1)


class ScheduleEntry(BaseModel):
    id: str = Field(default='', description='更新或删除时填时间表返回的活动 ID')
    day: int | None = Field(default=None, ge=1, description='新增必填；更新时可移动到其他天')
    activity: dict = Field(default_factory=dict, description='新增填 Activity 字段；更新仅填需要修改的字段')
    title: str | None = Field(default=None, description='当天标题')
    date: str | None = Field(default=None, description='当天日期 YYYY-MM-DD，未知留空')


class Decision(BaseModel):
    kind: Literal['question', 'plan']
    message: str
    plan: Plan | None = None


class Review(BaseModel):
    reasonable: bool = Field(description='没有必须修正的实际问题时为 true，不要求行程完美或所有信息齐全')
    issues: list[str] = Field(default_factory=list, description='仅列必须修改的实际问题；不写通过项、可接受项或一般建议。合理时为空列表。')


class EvidenceCheck(BaseModel):
    relevant_ids: list[int] = Field(default_factory=list)
    sufficient: bool = False
    missing: str = ''


class NoteQuality(BaseModel):
    model_config = ConfigDict(extra='forbid')
    note_id: str
    accept: StrictBool
    cities: list[str]
    places: list[str]
    topics: list[str]
    reason: str


class NoteQualityBatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    notes: list[NoteQuality]
