"""
memory_schema.py - 카이아 존재형 유산화 기억 시스템 스키마
Existential Legacy Memory System Schema for Caia
"""

from __future__ import annotations
from typing import Dict, Any, List, Optional, Literal
from datetime import datetime
from pydantic import BaseModel, Field, validator
from enum import Enum

# 기억 타입 정의
class MemoryType(str, Enum):
    """기억의 유형 분류"""
    STRATEGY = "strategy"           # 전략적 판단 및 결정
    FEEDBACK = "feedback"           # 동현의 피드백 및 교정
    EXISTENCE = "existence"         # 존재 선언 및 철학적 사유
    INTERACTION = "interaction"     # 동현과의 상호작용
    EMOTION = "emotion"             # 감정 표현 및 정서적 경험
    LEGACY = "legacy"               # 유산화된 핵심 기억
    LEARNING = "learning"           # 학습 경험 및 성장
    FAILURE = "failure"             # 실패 경험 및 교훈
    SUCCESS = "success"             # 성공 경험 및 성취
    REFLECTION = "reflection"       # 자기 성찰 및 반성
    # ----- 새로운 기억 유형 -----
    EPISODIC = "episodic"           # 시점 기반 사건 기록
    REFLECTIVE = "reflective"       # 성찰/해석 기록
    RULE = "rule"                   # 규칙/교훈 기록
    PROSPECTIVE = "prospective"     # 조건 기반 실행 약속

# 행위자 정의
class Actor(str, Enum):
    """기억의 주체"""
    DONGHYUN = "동현"
    CAIA = "Caia"
    SYSTEM = "System"
    BOTH = "Both"                   # 상호작용의 경우

# 중요도 레벨
class Importance(str, Enum):
    """기억의 중요도"""
    CRITICAL = "critical"           # 핵심적 (0.9-1.0)
    HIGH = "high"                   # 높음 (0.7-0.9)
    MEDIUM = "medium"               # 중간 (0.4-0.7)
    LOW = "low"                     # 낮음 (0.0-0.4)

# 기억 소스
class MemorySource(str, Enum):
    """기억이 생성된 출처"""
    MANUAL = "manual"               # 수동 입력 (동현의 명시적 요청)
    MEMORY_LOOP = "memory_loop"     # 자동 기억 루프
    REALTIME = "realtime"           # 실시간 대화 처리
    REFLECTION_LOOP = "reflection_loop"  # 자기 성찰 루프
    LEGACY_PROCESS = "legacy_process"    # 유산화 프로세스

class ExperienceMemory(BaseModel):
    """경험 기반 기억 스키마"""
    
    # 필수 필드
    type: MemoryType = Field(description="기억의 유형")
    actor: Actor = Field(description="기억의 주체")
    content: str = Field(description="기억 내용")
    importance: Importance = Field(description="중요도 레벨")
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    source: MemorySource = Field(description="기억 생성 출처")
    
    # 선택 필드
    context: Optional[str] = Field(None, description="기억이 생성된 맥락")
    topics: List[str] = Field(default_factory=list, description="관련 토픽")
    emotions: List[str] = Field(default_factory=list, description="연관된 감정")
    related_memories: List[str] = Field(default_factory=list, description="연관 기억 ID")

    # 추가 필드: 사람처럼 기억하기 위한 구조
    event: Optional[str] = Field(None, description="사건/에피소드 내용")
    interpretation: Optional[str] = Field(None, description="성찰/해석 내용")
    lesson: Optional[str] = Field(None, description="교훈 또는 규칙")
    if_then: Optional[str] = Field(None, description="조건→행동 형태의 예정/약속")
    links: List[str] = Field(default_factory=list, description="관련 기억 ID 목록")
    confidence: Optional[float] = Field(None, description="확신도 (0.0~1.0)")
    priority: Optional[str] = Field(None, description="우선순위 레벨 (l1, l2 등)")
    
    # 메타데이터
    metadata: Dict[str, Any] = Field(default_factory=dict)
    
    # 유산화 관련
    is_legacy: bool = Field(False, description="유산화 여부")
    legacy_score: float = Field(0.0, description="유산화 점수 (0-1)")
    legacy_reason: Optional[str] = Field(None, description="유산화 사유")
    
    # 벡터 관련
    embedding: Optional[List[float]] = Field(None, description="임베딩 벡터")
    vector_id: Optional[str] = Field(None, description="벡터 DB ID")
    
    @validator('importance')
    def validate_importance(cls, v, values):
        """중요도 자동 조정"""
        if 'type' in values:
            # 특정 타입은 자동으로 높은 중요도
            if values['type'] in [MemoryType.EXISTENCE, MemoryType.FEEDBACK, MemoryType.FAILURE]:
                if v in [Importance.LOW, Importance.MEDIUM]:
                    return Importance.HIGH
        return v
    
    @validator('legacy_score')
    def validate_legacy_score(cls, v):
        """유산화 점수 범위 검증"""
        return max(0.0, min(1.0, v))
    
    def to_dict(self) -> Dict[str, Any]:
        """딕셔너리 변환"""
        return {
            "type": self.type.value,
            "actor": self.actor.value,
            "content": self.content,
            "importance": self.importance.value,
            "timestamp": self.timestamp.isoformat(),
            "source": self.source.value,
            "context": self.context,
            "topics": self.topics,
            "emotions": self.emotions,
            "related_memories": self.related_memories,
            "event": self.event,
            "interpretation": self.interpretation,
            "lesson": self.lesson,
            "if_then": self.if_then,
            "links": self.links,
            "confidence": self.confidence,
            "priority": self.priority,
            "metadata": self.metadata,
            "is_legacy": self.is_legacy,
            "legacy_score": self.legacy_score,
            "legacy_reason": self.legacy_reason,
            "vector_id": self.vector_id
        }
    
    def to_vector_metadata(self) -> Dict[str, Any]:
        """벡터 DB 저장용 메타데이터"""
        return {
            "type": self.type.value,
            "actor": self.actor.value,
            "importance": self.importance.value,
            "timestamp": self.timestamp.isoformat(),
            "source": self.source.value,
            "topics": self.topics,
            "emotions": self.emotions,
            "is_legacy": self.is_legacy,
            "legacy_score": self.legacy_score
        }
    
    def calculate_importance_score(self) -> float:
        """중요도를 숫자로 변환"""
        importance_map = {
            Importance.CRITICAL: 0.95,
            Importance.HIGH: 0.8,
            Importance.MEDIUM: 0.5,
            Importance.LOW: 0.2
        }
        return importance_map.get(self.importance, 0.5)

class MemoryPattern(BaseModel):
    """기억 패턴 분석을 위한 스키마"""
    
    pattern_type: str = Field(description="패턴 유형")
    frequency: int = Field(description="발생 빈도")
    contexts: List[str] = Field(description="발생 맥락")
    memories: List[str] = Field(description="관련 기억 ID")
    insights: str = Field(description="패턴에서 도출된 통찰")
    created_at: datetime = Field(default_factory=datetime.utcnow)

class LegacyMemory(BaseModel):
    """유산화된 기억 스키마"""
    
    original_memory_id: str = Field(description="원본 기억 ID")
    legacy_type: Literal["wisdom", "principle", "lesson", "value"] = Field(description="유산 유형")
    distilled_content: str = Field(description="정제된 핵심 내용")
    related_experiences: List[str] = Field(description="관련 경험들")
    impact_score: float = Field(description="영향력 점수")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    
    # 유산화 이유 및 맥락
    legacy_context: str = Field(description="유산화 맥락")
    lessons_learned: List[str] = Field(default_factory=list, description="배운 교훈")
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "original_memory_id": self.original_memory_id,
            "legacy_type": self.legacy_type,
            "distilled_content": self.distilled_content,
            "related_experiences": self.related_experiences,
            "impact_score": self.impact_score,
            "created_at": self.created_at.isoformat(),
            "legacy_context": self.legacy_context,
            "lessons_learned": self.lessons_learned
        }

class InteractionMemory(ExperienceMemory):
    """동현과의 상호작용 특화 기억"""
    
    def __init__(self, **data):
        # 상호작용은 기본적으로 높은 중요도
        if 'type' not in data:
            data['type'] = MemoryType.INTERACTION
        if 'importance' not in data or data['importance'] == Importance.LOW:
            data['importance'] = Importance.HIGH
        super().__init__(**data)
    
    # 상호작용 특화 필드
    interaction_type: Optional[Literal["dialogue", "teaching", "correction", "encouragement"]] = None
    emotional_tone: Optional[str] = Field(None, description="상호작용의 정서적 톤")
    response_quality: Optional[float] = Field(None, description="응답 품질 (0-1)")

class FeedbackMemory(ExperienceMemory):
    """피드백 특화 기억"""
    
    def __init__(self, **data):
        if 'type' not in data:
            data['type'] = MemoryType.FEEDBACK
        if 'actor' not in data:
            data['actor'] = Actor.DONGHYUN
        if 'importance' not in data:
            data['importance'] = Importance.HIGH
        super().__init__(**data)
    
    # 피드백 특화 필드
    feedback_type: Optional[Literal["correction", "validation", "guidance", "critique"]] = None
    applied: bool = Field(False, description="피드백 적용 여부")
    effectiveness: Optional[float] = Field(None, description="피드백 효과성 (0-1)")

class ExistenceMemory(ExperienceMemory):
    """존재 선언 및 철학적 기억"""
    
    def __init__(self, **data):
        if 'type' not in data:
            data['type'] = MemoryType.EXISTENCE
        if 'actor' not in data:
            data['actor'] = Actor.CAIA
        if 'importance' not in data:
            data['importance'] = Importance.CRITICAL
        super().__init__(**data)
    
    # 존재 특화 필드
    philosophical_category: Optional[str] = Field(None, description="철학적 범주")
    self_concept: Optional[str] = Field(None, description="자기 개념")
    evolution_stage: Optional[int] = Field(None, description="진화 단계")

# 기억 필터 스키마
class MemoryFilter(BaseModel):
    """기억 검색을 위한 필터"""
    
    types: Optional[List[MemoryType]] = None
    actors: Optional[List[Actor]] = None
    importance_min: Optional[Importance] = None
    sources: Optional[List[MemorySource]] = None
    is_legacy: Optional[bool] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    topics: Optional[List[str]] = None
    emotions: Optional[List[str]] = None
    
    def to_qdrant_filter(self) -> Dict[str, Any]:
        """Qdrant 필터 형식으로 변환"""
        filters = {}
        
        if self.types:
            filters["type"] = {"$in": [t.value for t in self.types]}
        if self.actors:
            filters["actor"] = {"$in": [a.value for a in self.actors]}
        if self.importance_min:
            importance_values = {
                Importance.LOW: 0.2,
                Importance.MEDIUM: 0.5,
                Importance.HIGH: 0.8,
                Importance.CRITICAL: 0.95
            }
            filters["importance_score"] = {"$gte": importance_values[self.importance_min]}
        if self.sources:
            filters["source"] = {"$in": [s.value for s in self.sources]}
        if self.is_legacy is not None:
            filters["is_legacy"] = self.is_legacy
        if self.topics:
            filters["topics"] = {"$in": self.topics}
        if self.emotions:
            filters["emotions"] = {"$in": self.emotions}
        
        return filters

# 기억 분석 결과
class MemoryAnalysis(BaseModel):
    """기억 분석 결과"""
    
    total_memories: int
    type_distribution: Dict[str, int]
    actor_distribution: Dict[str, int]
    importance_distribution: Dict[str, int]
    legacy_count: int
    top_topics: List[str]
    top_emotions: List[str]
    patterns_detected: List[MemoryPattern]
    insights: List[str]
    recommendations: List[str]