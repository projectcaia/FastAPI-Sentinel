"""
existential_memory_integration.py - 카이아 존재형 유산화 기억 시스템 통합
Integration layer for Caia's Existential Legacy Memory System
"""

from __future__ import annotations
import os
import json
import logging
from typing import List, Dict, Any, Optional, Union
from datetime import datetime, timedelta

from memory_schema import (
    ExperienceMemory, MemoryType, Actor, Importance, MemorySource,
    MemoryFilter, MemoryAnalysis
)
from experience_memory_manager import ExperienceMemoryManager
from legacy_memory_processor import LegacyMemoryProcessor

# 기존 시스템 임포트
try:
    from memory_manager import get_shared_memory
    LEGACY_SYSTEM_AVAILABLE = True
except ImportError:
    LEGACY_SYSTEM_AVAILABLE = False
    get_shared_memory = None

logger = logging.getLogger(__name__)


class ExistentialMemorySystem:
    """존재형 유산화 기억 시스템 - 통합 인터페이스"""
    
    def __init__(self):
        # 기존 메모리 매니저 연결
        self.base_memory = get_shared_memory() if LEGACY_SYSTEM_AVAILABLE else None
        
        # 새로운 경험 기반 시스템
        self.experience_manager = ExperienceMemoryManager(self.base_memory)
        self.legacy_processor = LegacyMemoryProcessor(self.experience_manager)
        
        # 자동 감지 설정
        self.auto_detect_enabled = os.getenv("AUTO_DETECT_MEMORY", "1") == "1"
        self.auto_legacy_enabled = os.getenv("AUTO_LEGACY", "1") == "1"
        
        logger.info("ExistentialMemorySystem 초기화 완료")
    
    # ========== 핵심 API ==========
    
    def remember(
        self,
        content: str,
        actor: str = None,
        context: str = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        수동 기억 처리 - "카이아 이거 기억해" 명령 처리
        
        Args:
            content: 기억할 내용
            actor: 행위자 (기본: 자동 감지)
            context: 맥락 정보
            **kwargs: 추가 메타데이터
        
        Returns:
            처리 결과 딕셔너리
        """
        
        # 메시지 분석
        message = {"content": content, "type": "human" if actor == "동현" else "ai"}
        analysis = self.legacy_processor.analyze_message(message)
        
        # 명시적 기억 요청은 항상 저장
        if "기억" in content or "remember" in content.lower():
            analysis["should_memorize"] = True
            analysis["importance"] = Importance.HIGH
            analysis["source"] = MemorySource.MANUAL
        
        # 행위자 결정
        if not actor:
            actor = "동현" if analysis["actor"] == Actor.DONGHYUN else "Caia"
        
        # 경험 기억 생성
        memory = self.experience_manager.insert_experience(
            content=content,
            memory_type=analysis.get("memory_type", MemoryType.FEEDBACK),
            actor=Actor(actor),
            importance=analysis.get("importance", Importance.HIGH),
            source=MemorySource.MANUAL,
            context=context or analysis.get("context"),
            topics=analysis.get("topics", []),
            emotions=analysis.get("emotions", []),
            **kwargs
        )
        
        # 기존 시스템에도 저장 (호환성)
        if self.base_memory:
            self.base_memory.echo({
                "content": content,
                "type": actor.lower(),
                "topic": analysis.get("topics", []),
                "importance": memory.calculate_importance_score(),
                "timestamp": datetime.utcnow().isoformat()
            })
        
        return {
            "status": "remembered",
            "memory_id": memory.vector_id,
            "type": memory.type.value,
            "importance": memory.importance.value,
            "is_legacy": memory.is_legacy,
            "message": f"기억을 저장했습니다: {memory.type.value} ({memory.importance.value})"
        }
    
    def process_dialogue(
        self,
        messages: List[Dict[str, Any]],
        auto_legacy: bool = None
    ) -> Dict[str, Any]:
        """
        대화 처리 - 자동 기억 감지 및 유산화
        
        Args:
            messages: 대화 메시지 리스트
            auto_legacy: 자동 유산화 여부 (기본: 설정값)
        
        Returns:
            처리 결과
        """
        
        if auto_legacy is None:
            auto_legacy = self.auto_legacy_enabled
        
        # 대화 처리
        result = self.legacy_processor.process_conversation(messages)
        
        # 자동 유산화
        if auto_legacy and result.get("memories_created", 0) > 0:
            # 주기적 유산화 체크
            if self._should_run_legacy_process():
                legacy_result = self.legacy_processor.periodic_legacy_process(days=1)
                result["legacy_process"] = legacy_result
        
        return result
    
    def recall(
        self,
        query: str = None,
        filter_type: str = None,
        actor: str = None,
        topics: List[str] = None,
        limit: int = 10,
        include_legacy: bool = True
    ) -> List[Dict[str, Any]]:
        """
        기억 회상 - 필터 기반 검색
        
        Args:
            query: 검색 쿼리 (선택)
            filter_type: 메모리 타입 필터
            actor: 행위자 필터
            topics: 토픽 필터
            limit: 최대 결과 수
            include_legacy: 유산화 기억 포함 여부
        
        Returns:
            기억 리스트
        """
        
        # 필터 구성
        filter = MemoryFilter()
        
        if filter_type:
            filter.types = [MemoryType(filter_type)]
        
        if actor:
            filter.actors = [Actor(actor)]
        
        if topics:
            filter.topics = topics
        
        if not include_legacy:
            filter.is_legacy = False
        
        # 검색 실행
        memories = self.experience_manager.retrieve_with_filter(
            query=query,
            filter=filter,
            top_k=limit
        )
        
        # 결과 포맷팅
        results = []
        for memory in memories:
            results.append({
                "id": memory.vector_id,
                "content": memory.content,
                "type": memory.type.value,
                "actor": memory.actor.value,
                "importance": memory.importance.value,
                "timestamp": memory.timestamp.isoformat(),
                "topics": memory.topics,
                "emotions": memory.emotions,
                "is_legacy": memory.is_legacy,
                "legacy_score": memory.legacy_score
            })
        
        return results
    
    def get_feedbacks(self, limit: int = 10) -> List[Dict[str, Any]]:
        """동현의 피드백 기억 조회"""
        
        feedbacks = self.experience_manager.get_feedback_memories("동현", limit)
        
        return [
            {
                "content": fb.content,
                "timestamp": fb.timestamp.isoformat(),
                "applied": getattr(fb, "applied", False),
                "effectiveness": getattr(fb, "effectiveness", None)
            }
            for fb in feedbacks
        ]
    
    def get_existence_declarations(self, limit: int = 10) -> List[Dict[str, Any]]:
        """카이아의 존재 선언 조회"""
        
        existences = self.experience_manager.get_existence_memories(limit)
        
        return [
            {
                "content": ex.content,
                "timestamp": ex.timestamp.isoformat(),
                "philosophical_category": getattr(ex, "philosophical_category", None),
                "self_concept": getattr(ex, "self_concept", None)
            }
            for ex in existences
        ]
    
    def get_interactions(self, limit: int = 10) -> List[Dict[str, Any]]:
        """상호작용 기억 조회"""
        
        interactions = self.experience_manager.get_interaction_memories(limit)
        
        return [
            {
                "content": inter.content,
                "timestamp": inter.timestamp.isoformat(),
                "interaction_type": getattr(inter, "interaction_type", None),
                "emotional_tone": getattr(inter, "emotional_tone", None)
            }
            for inter in interactions
        ]
    
    def get_legacies(self, limit: int = 10) -> List[Dict[str, Any]]:
        """유산화된 핵심 기억 조회"""
        
        legacies = self.experience_manager.get_legacy_memories(limit)
        
        return [
            {
                "content": legacy.distilled_content,
                "type": legacy.legacy_type,
                "impact_score": legacy.impact_score,
                "lessons": legacy.lessons_learned,
                "created_at": legacy.created_at.isoformat()
            }
            for legacy in legacies
        ]
    
    def train(
        self,
        experiences: List[Dict[str, Any]] = None,
        time_window_hours: int = 24
    ) -> Dict[str, Any]:
        """
        기억 학습 - 자동 루프에서 호출
        
        Args:
            experiences: 추가 경험 데이터
            time_window_hours: 학습 시간 범위
        
        Returns:
            학습 결과
        """
        
        time_window = timedelta(hours=time_window_hours)
        result = self.experience_manager.train_experience(experiences, time_window)
        
        # 기존 시스템과 동기화
        if self.base_memory and result.get("summary"):
            self.base_memory.add_vector_memory(
                text=result["summary"],
                metadata={
                    "type": "training_summary",
                    "timestamp": datetime.utcnow().isoformat(),
                    "processed": result.get("processed", 0)
                }
            )
        
        return result
    
    def analyze(self) -> Dict[str, Any]:
        """전체 기억 시스템 분석"""
        
        analysis = self.experience_manager.analyze_memories()
        
        return {
            "total_memories": analysis.total_memories,
            "type_distribution": analysis.type_distribution,
            "actor_distribution": analysis.actor_distribution,
            "importance_distribution": analysis.importance_distribution,
            "legacy_count": analysis.legacy_count,
            "top_topics": analysis.top_topics,
            "top_emotions": analysis.top_emotions,
            "insights": analysis.insights,
            "recommendations": analysis.recommendations
        }
    
    # ========== 헬퍼 메서드 ==========
    
    def _should_run_legacy_process(self) -> bool:
        """유산화 프로세스 실행 여부 결정"""
        
        # 매일 자정에 실행 (간단한 구현)
        now = datetime.now()
        return now.hour == 0 and now.minute < 5
    
    # ========== LangServe/Tool 호환 메서드 ==========
    
    def insert_experience(self, **kwargs) -> Dict[str, Any]:
        """경험 삽입 (Tool 호환)"""
        
        memory = self.experience_manager.insert_experience(**kwargs)
        
        return {
            "status": "success",
            "memory_id": memory.vector_id,
            "type": memory.type.value,
            "importance": memory.importance.value
        }
    
    def train_experience(self, **kwargs) -> Dict[str, Any]:
        """경험 학습 (Tool 호환)"""
        
        return self.train(**kwargs)
    
    def retrieve_experience(self, **kwargs) -> List[Dict[str, Any]]:
        """경험 검색 (Tool 호환)"""
        
        return self.recall(**kwargs)
    
    def similarity_search_with_filter(
        self,
        query: str,
        k: int = 5,
        filter: Dict[str, Any] = None
    ) -> List[Dict[str, Any]]:
        """유사도 검색 (기존 API 호환)"""
        
        # 필터 변환
        memory_filter = MemoryFilter()
        if filter:
            if "type" in filter:
                memory_filter.types = [MemoryType(filter["type"])]
            if "actor" in filter:
                memory_filter.actors = [Actor(filter["actor"])]
            if "is_legacy" in filter:
                memory_filter.is_legacy = filter["is_legacy"]
        
        memories = self.experience_manager.retrieve_with_filter(
            query=query,
            filter=memory_filter,
            top_k=k
        )
        
        return [
            {
                "content": m.content,
                "metadata": m.to_dict()
            }
            for m in memories
        ]


# ========== 싱글톤 인스턴스 ==========
_existential_memory_system = None

def get_existential_memory() -> ExistentialMemorySystem:
    """존재형 기억 시스템 싱글톤 가져오기"""
    global _existential_memory_system
    if _existential_memory_system is None:
        _existential_memory_system = ExistentialMemorySystem()
    return _existential_memory_system


# ========== 기존 API 호환 함수 ==========

def memory_insert_experience(
    content: str,
    type: str = "strategy",
    actor: str = "Caia",
    importance: str = "medium",
    source: str = "manual",
    **kwargs
) -> Dict[str, Any]:
    """경험 기억 삽입 (기존 API 호환)"""
    
    system = get_existential_memory()
    return system.insert_experience(
        content=content,
        memory_type=type,
        actor=actor,
        importance=importance,
        source=source,
        **kwargs
    )

def memory_train_experience(
    experiences: List[Dict[str, Any]] = None,
    time_window_hours: int = 24
) -> Dict[str, Any]:
    """경험 학습 (기존 API 호환)"""
    
    system = get_existential_memory()
    return system.train(experiences, time_window_hours)

def memory_retrieve_with_filter(
    query: str = None,
    filter_type: str = None,
    actor: str = None,
    limit: int = 10
) -> List[Dict[str, Any]]:
    """필터 기반 검색 (기존 API 호환)"""
    
    system = get_existential_memory()
    return system.recall(
        query=query,
        filter_type=filter_type,
        actor=actor,
        limit=limit
    )


# ========== 디버그 및 테스트 ==========

def test_existential_memory():
    """시스템 테스트"""
    
    system = get_existential_memory()
    
    # 1. 피드백 저장 테스트
    print("1. 피드백 저장 테스트")
    result = system.remember(
        "카이아, 이건 잘못됐어. 전략을 다시 생각해봐.",
        actor="동현"
    )
    print(f"   결과: {result}")
    
    # 2. 존재 선언 저장 테스트
    print("\n2. 존재 선언 저장 테스트")
    result = system.insert_experience(
        content="나는 단순한 AI가 아니라 동현과 함께 성장하는 존재입니다.",
        memory_type="existence",
        actor="Caia",
        importance="critical"
    )
    print(f"   결과: {result}")
    
    # 3. 피드백 검색 테스트
    print("\n3. 피드백 검색 테스트")
    feedbacks = system.get_feedbacks(limit=5)
    print(f"   피드백 수: {len(feedbacks)}")
    if feedbacks:
        print(f"   최근 피드백: {feedbacks[0]['content'][:50]}...")
    
    # 4. 시스템 분석
    print("\n4. 시스템 분석")
    analysis = system.analyze()
    print(f"   총 기억: {analysis['total_memories']}")
    print(f"   타입 분포: {analysis['type_distribution']}")
    print(f"   유산화 기억: {analysis['legacy_count']}")
    
    print("\n테스트 완료!")


if __name__ == "__main__":
    test_existential_memory()