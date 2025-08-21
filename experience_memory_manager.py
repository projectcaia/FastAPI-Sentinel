"""
experience_memory_manager.py - 카이아 경험 기반 메모리 매니저
Experience-based Memory Manager for Caia's Existential Legacy System
"""

from __future__ import annotations
import os
import json
import hashlib
import logging
from typing import List, Dict, Any, Optional, Union
from datetime import datetime, timedelta
from collections import defaultdict, Counter

from langchain_openai import OpenAIEmbeddings
from langchain_community.vectorstores import Qdrant as LC_Qdrant
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels
from openai import OpenAI

from memory_schema import (
    ExperienceMemory, MemoryType, Actor, Importance, MemorySource,
    FeedbackMemory, InteractionMemory, ExistenceMemory,
    MemoryFilter, MemoryAnalysis, LegacyMemory, MemoryPattern
)

logger = logging.getLogger(__name__)


class ExperienceMemoryManager:
    """존재형 유산화 기억 시스템 매니저"""
    
    def __init__(self, base_memory_manager=None):
        self.base_memory = base_memory_manager
        self.client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self.embeddings = OpenAIEmbeddings(
            model=os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
        )
        
        # Qdrant 설정
        self.qdrant_url = os.getenv("QDRANT_URL")
        self.qdrant_api_key = os.getenv("QDRANT_API_KEY")
        self.collection_name = os.getenv("QDRANT_COLLECTION", "caia-memory")
        self.qdrant_client = QdrantClient(url=self.qdrant_url, api_key=self.qdrant_api_key)
        
        # 기억 저장소
        self.experiences: Dict[str, ExperienceMemory] = {}
        self.legacy_memories: Dict[str, LegacyMemory] = {}
        self.patterns: List[MemoryPattern] = []
        
        # 트리거 패턴
        self.feedback_triggers = ["기억해", "잘못", "수정", "아니야", "틀렸", "다시", "피드백"]
        self.existence_triggers = ["나는", "존재", "철학", "생각", "느낌", "감정", "의미"]
        self.interaction_triggers = ["동현", "너", "우리", "함께", "대화"]
        
        # 통계
        self.stats = defaultdict(int)
        
        logger.info("ExperienceMemoryManager 초기화 완료")
    
    def insert_experience(
        self,
        content: str,
        memory_type: Union[str, MemoryType],
        actor: Union[str, Actor],
        importance: Union[str, Importance] = None,
        source: Union[str, MemorySource] = None,
        context: Optional[str] = None,
        topics: Optional[List[str]] = None,
        emotions: Optional[List[str]] = None,
        **kwargs
    ) -> ExperienceMemory:
        """
        새로운 경험 기억을 삽입한다.

        Caia의 사람처럼 기억하기 위한 구조를 지원하기 위해, 이벤트(event), 성찰(interpretation), 교훈(lesson),
        예정/약속(if_then) 등의 필드를 kwargs로 전달할 수 있다. 임베딩 생성 시에는 이러한 부가 필드를
        함께 고려하여 기억의 의미를 더 잘 포착한다.
        """

        # 타입 변환
        if isinstance(memory_type, str):
            memory_type = MemoryType(memory_type)
        if isinstance(actor, str):
            actor = Actor(actor)
        if isinstance(importance, str):
            importance = Importance(importance)
        if isinstance(source, str):
            source = MemorySource(source)

        # 자동 변환 옵션: 성찰/교훈/트리거 생성
        allow_transform = True
        if 'allow_transform' in kwargs:
            allow_transform = bool(kwargs.pop('allow_transform'))

        # event 기본값: content를 이벤트로 해석
        event_text = kwargs.get('event') or content

        # 자동 성찰/규칙/트리거 생성: interpretation, lesson, if_then
        if allow_transform and event_text:
            # interpretation 생성
            if not kwargs.get('interpretation'):
                kwargs['interpretation'] = self._generate_interpretation(event_text)
            # lesson 생성
            if not kwargs.get('lesson'):
                kwargs['lesson'] = self._generate_lesson(kwargs.get('interpretation') or event_text)
            # if_then 생성
            if not kwargs.get('if_then'):
                kwargs['if_then'] = self._generate_if_then(kwargs.get('lesson'))

        # 중요도 자동 판단
        if not importance:
            importance = self._calculate_importance(content, memory_type, actor)

        # 소스 기본값
        if not source:
            source = MemorySource.MANUAL

        # 토픽 자동 추출
        if not topics:
            topics = self._extract_topics(content, memory_type)

        # 감정 자동 추출
        if not emotions:
            emotions = self._extract_emotions(content, memory_type)

        # 특화 메모리 생성: 기존 특화 클래스 우선, 나머지는 ExperienceMemory
        if memory_type == MemoryType.FEEDBACK:
            memory = FeedbackMemory(
                content=content,
                actor=actor,
                importance=importance,
                source=source,
                context=context,
                topics=topics,
                emotions=emotions,
                **kwargs
            )
        elif memory_type == MemoryType.INTERACTION:
            memory = InteractionMemory(
                content=content,
                actor=actor,
                importance=importance,
                source=source,
                context=context,
                topics=topics,
                emotions=emotions,
                **kwargs
            )
        elif memory_type == MemoryType.EXISTENCE:
            memory = ExistenceMemory(
                content=content,
                actor=actor,
                importance=importance,
                source=source,
                context=context,
                topics=topics,
                emotions=emotions,
                **kwargs
            )
        else:
            memory = ExperienceMemory(
                type=memory_type,
                content=content,
                actor=actor,
                importance=importance,
                source=source,
                context=context,
                topics=topics,
                emotions=emotions,
                **kwargs
            )

        # Duplicate detection & merge: if an existing memory with identical content and type exists, merge and return
        duplicate_id = self._find_duplicate(content, memory_type)
        if duplicate_id:
            # Merge new data into existing memory and return it
            existing = self.experiences[duplicate_id]
            new_data = {
                "topics": topics,
                "emotions": emotions,
                "event": kwargs.get("event"),
                "interpretation": kwargs.get("interpretation"),
                "lesson": kwargs.get("lesson"),
                "if_then": kwargs.get("if_then"),
                "links": kwargs.get("links", []),
                "confidence": kwargs.get("confidence"),
                "priority": kwargs.get("priority"),
            }
            self._merge_memories(existing, new_data)
            # stats update: no new count, but we can increment a merged counter
            self.stats.setdefault("merged", 0)
            self.stats["merged"] += 1
            logger.info(
                f"경험 기억 병합: type={memory_type.value}, actor={actor.value}, merged_with={duplicate_id}"
            )
            return existing

        # 임베딩 생성: content 외에도 event, interpretation, lesson, if_then을 포함하여 의미를 보강
        extra_texts = []
        for field_name in ["event", "interpretation", "lesson", "if_then"]:
            val = kwargs.get(field_name)
            if val:
                extra_texts.append(str(val))
        embedding_input = " \n ".join([content] + extra_texts)
        try:
            embedding = self.embeddings.embed_query(embedding_input)
            memory.embedding = embedding
        except Exception:
            # 임베딩 실패 시 content만 사용
            embedding = self.embeddings.embed_query(content)
            memory.embedding = embedding

        # 벡터 DB에 저장
        vector_id = self._store_to_vector_db(memory)
        memory.vector_id = vector_id

        # 로컬 저장소에 추가
        memory_id = hashlib.md5(f"{content}{datetime.utcnow().isoformat()}".encode()).hexdigest()
        self.experiences[memory_id] = memory

        # 통계 업데이트
        self.stats[f"type_{memory_type.value}"] += 1
        self.stats[f"actor_{actor.value}"] += 1
        self.stats["total_experiences"] += 1

        logger.info(
            f"경험 기억 저장: type={memory_type.value}, actor={actor.value}, importance={importance.value}"
        )

        # 유산화 체크
        if self._should_legacy(memory):
            self._create_legacy(memory_id, memory)

        return memory

    def _find_duplicate(self, content: str, memory_type: MemoryType) -> Optional[str]:
        """내용과 타입이 동일한 기존 기억을 찾는다. Exact match 기준."""
        key_content = content.strip()
        for mid, exp in self.experiences.items():
            try:
                if exp.type == memory_type and (exp.content or "").strip() == key_content:
                    return mid
            except Exception:
                continue
        return None

    def _merge_memories(self, existing: ExperienceMemory, new_data: Dict[str, Any]) -> None:
        """기존 기억과 신규 데이터를 병합한다. 주제/감정/링크/교훈 등 누적."""
        # 병합 가능한 필드: topics, emotions, links
        existing.topics = list(set((existing.topics or []) + (new_data.get("topics") or [])))
        existing.emotions = list(set((existing.emotions or []) + (new_data.get("emotions") or [])))
        # lesson/if_then/interpretation/event: concatenate if new provided
        for field_name in ["lesson", "if_then", "interpretation", "event"]:
            new_val = new_data.get(field_name)
            if new_val:
                old_val = getattr(existing, field_name, None)
                if old_val and old_val != new_val:
                    # concatenate with separator
                    setattr(existing, field_name, f"{old_val}\n{new_val}")
                else:
                    setattr(existing, field_name, new_val)
        # links
        existing.links = list(dict.fromkeys((existing.links or []) + (new_data.get("links") or [])))
        # confidence/priority: take max confidence, highest priority (string compare)
        if new_data.get("confidence") is not None:
            if existing.confidence is None:
                existing.confidence = new_data["confidence"]
            else:
                existing.confidence = max(existing.confidence, new_data["confidence"])
        if new_data.get("priority"):
            if existing.priority:
                # choose lexicographically smaller (e.g. l1 < l2)
                existing.priority = min(existing.priority, new_data["priority"])
            else:
                existing.priority = new_data["priority"]
        # timestamp: update to latest
        existing.timestamp = datetime.utcnow()

    def recall_rules(self, query: str, limit: int = 10) -> List[ExperienceMemory]:
        """
        규칙(rule) 또는 예정(prospective) 타입 기억을 쿼리로 검색하여 반환한다.
        Qdrant 벡터 검색을 사용하며, type 필터를 rule/prospective로 제한한다.
        """
        if not query:
            return []
        try:
            # 임베딩 생성
            q_vec = self.embeddings.embed_query(query)
        except Exception:
            return []

        # Qdrant 검색은 필터 없이 먼저 수행한 후, 애플리케이션 레벨에서 type 필터링을 적용한다.
        # limit를 넉넉하게 가져와 필터링 후 원하는 개수만큼 반환한다.
        q_limit = max(limit * 3, 10)
        try:
            search_result = self.qdrant_client.search(
                collection_name=self.collection_name,
                query_vector=q_vec,
                limit=q_limit,
                with_payload=True
            )
        except Exception:
            return []
        out: List[ExperienceMemory] = []
        for point in search_result:
            vector_id = point.id
            # find memory by vector id
            found = None
            for mid, exp in self.experiences.items():
                if exp.vector_id == vector_id:
                    found = exp
                    break
            if found and found.type in (MemoryType.RULE, MemoryType.PROSPECTIVE):
                out.append(found)
                if len(out) >= limit:
                    break
        return out

    # ───────── 변환 생성기 ─────────
    def _generate_interpretation(self, event: str) -> str:
        """
        간단한 성찰/해석 생성기. 실제 구현에서는 LLM을 호출하거나 규칙 기반 해석을 해야 하지만,
        여기서는 입력 이벤트를 기반으로 기본적인 해석을 생성한다.
        """
        if not event:
            return ""
        # 간단한 규칙: 첫 문장을 그대로 인용하여 '해석' 프리픽스 추가
        sent = event.strip().split(". ")[0].strip()
        return f"성찰: {sent}"

    def _generate_lesson(self, interpretation: str) -> str:
        """
        간단한 교훈/규칙 생성기. 해석을 기반으로 교훈 문장을 만든다.
        """
        if not interpretation:
            return ""
        base = interpretation.replace("성찰:", "").strip()
        return f"교훈: {base}에서 배운 원칙을 기억하세요"

    def _generate_if_then(self, lesson: str) -> str:
        """
        간단한 조건→행동 생성기. 교훈을 기반으로 조건-행동 문장을 만든다.
        """
        if not lesson:
            return ""
        rule_text = lesson.replace("교훈:", "").strip()
        return f"만약 {rule_text} 상황이 발생하면, 이전 교훈을 적용하세요"
    
    def train_experience(
        self,
        experiences: List[Dict[str, Any]] = None,
        time_window: timedelta = None
    ) -> Dict[str, Any]:
        """경험 기억 학습 및 패턴 분석"""
        
        if not time_window:
            time_window = timedelta(days=1)
        
        cutoff = datetime.utcnow() - time_window
        
        # 시간 범위 내 경험 수집
        recent_experiences = []
        for exp_id, exp in self.experiences.items():
            if exp.timestamp >= cutoff:
                recent_experiences.append(exp)
        
        # 추가 경험 처리
        if experiences:
            for exp_data in experiences:
                exp = self.insert_experience(**exp_data)
                recent_experiences.append(exp)
        
        # 패턴 분석
        patterns = self._analyze_patterns(recent_experiences)
        self.patterns.extend(patterns)
        
        # 중요 경험 유산화
        legacy_candidates = [exp for exp in recent_experiences 
                            if exp.importance in [Importance.HIGH, Importance.CRITICAL]]
        
        legacies_created = []
        for exp in legacy_candidates:
            if self._should_legacy(exp):
                legacy = self._create_legacy(exp.vector_id, exp)
                if legacy:
                    legacies_created.append(legacy)
        
        # 학습 요약 생성
        summary = self._generate_training_summary(recent_experiences, patterns, legacies_created)
        
        # 학습 결과를 메모리로 저장
        self.insert_experience(
            content=summary,
            memory_type=MemoryType.LEARNING,
            actor=Actor.SYSTEM,
            importance=Importance.HIGH,
            source=MemorySource.MEMORY_LOOP,
            context=f"Training session at {datetime.utcnow().isoformat()}"
        )
        
        return {
            "processed": len(recent_experiences),
            "patterns_found": len(patterns),
            "legacies_created": len(legacies_created),
            "summary": summary
        }
    
    def retrieve_with_filter(
        self,
        query: str = None,
        filter: MemoryFilter = None,
        top_k: int = 10
    ) -> List[ExperienceMemory]:
        """필터 기반 기억 검색"""
        
        if not filter:
            filter = MemoryFilter()
        
        # Qdrant 필터 변환
        qdrant_filter = self._build_qdrant_filter(filter)
        
        if query:
            # 벡터 검색
            query_embedding = self.embeddings.embed_query(query)
            
            results = self.qdrant_client.search(
                collection_name=self.collection_name,
                query_vector=query_embedding,
                limit=top_k,
                query_filter=qdrant_filter,
                with_payload=True
            )
        else:
            # 필터만으로 검색
            results = self.qdrant_client.scroll(
                collection_name=self.collection_name,
                scroll_filter=qdrant_filter,
                limit=top_k,
                with_payload=True
            )[0]
        
        # 결과를 ExperienceMemory로 변환
        memories = []
        for result in results:
            payload = result.payload if hasattr(result, 'payload') else result
            memory = self._payload_to_memory(payload)
            if memory:
                memories.append(memory)
        
        return memories
    
    def get_feedback_memories(self, actor: str = "동현", limit: int = 10) -> List[FeedbackMemory]:
        """피드백 기억 조회"""
        filter = MemoryFilter(
            types=[MemoryType.FEEDBACK],
            actors=[Actor(actor)]
        )
        memories = self.retrieve_with_filter(filter=filter, top_k=limit)
        return [m for m in memories if isinstance(m, FeedbackMemory)]
    
    def get_existence_memories(self, limit: int = 10) -> List[ExistenceMemory]:
        """존재 선언 기억 조회"""
        filter = MemoryFilter(
            types=[MemoryType.EXISTENCE],
            actors=[Actor.CAIA]
        )
        memories = self.retrieve_with_filter(filter=filter, top_k=limit)
        return [m for m in memories if isinstance(m, ExistenceMemory)]
    
    def get_interaction_memories(self, limit: int = 10) -> List[InteractionMemory]:
        """상호작용 기억 조회"""
        filter = MemoryFilter(
            types=[MemoryType.INTERACTION]
        )
        memories = self.retrieve_with_filter(filter=filter, top_k=limit)
        return [m for m in memories if isinstance(m, InteractionMemory)]
    
    def get_legacy_memories(self, limit: int = 10) -> List[LegacyMemory]:
        """유산화된 기억 조회"""
        legacies = list(self.legacy_memories.values())
        legacies.sort(key=lambda x: x.impact_score, reverse=True)
        return legacies[:limit]
    
    def analyze_memories(self) -> MemoryAnalysis:
        """전체 기억 분석"""
        
        # 통계 수집
        type_dist = defaultdict(int)
        actor_dist = defaultdict(int)
        importance_dist = defaultdict(int)
        all_topics = []
        all_emotions = []
        
        for exp in self.experiences.values():
            type_dist[exp.type.value] += 1
            actor_dist[exp.actor.value] += 1
            importance_dist[exp.importance.value] += 1
            all_topics.extend(exp.topics)
            all_emotions.extend(exp.emotions)
        
        # 상위 토픽과 감정
        topic_counter = Counter(all_topics)
        emotion_counter = Counter(all_emotions)
        
        # 통찰 생성
        insights = self._generate_insights(
            type_dist, actor_dist, importance_dist,
            topic_counter, emotion_counter
        )
        
        # 추천 생성
        recommendations = self._generate_recommendations(insights)
        
        return MemoryAnalysis(
            total_memories=len(self.experiences),
            type_distribution=dict(type_dist),
            actor_distribution=dict(actor_dist),
            importance_distribution=dict(importance_dist),
            legacy_count=len(self.legacy_memories),
            top_topics=[t for t, _ in topic_counter.most_common(10)],
            top_emotions=[e for e, _ in emotion_counter.most_common(10)],
            patterns_detected=self.patterns[-10:],  # 최근 10개 패턴
            insights=insights,
            recommendations=recommendations
        )
    
    def _calculate_importance(
        self,
        content: str,
        memory_type: MemoryType,
        actor: Actor
    ) -> Importance:
        """중요도 자동 계산"""
        
        # 타입별 기본 중요도
        type_importance = {
            MemoryType.EXISTENCE: Importance.CRITICAL,
            MemoryType.FEEDBACK: Importance.HIGH,
            MemoryType.FAILURE: Importance.HIGH,
            MemoryType.LEGACY: Importance.CRITICAL,
            MemoryType.INTERACTION: Importance.HIGH,
            MemoryType.STRATEGY: Importance.MEDIUM,
            MemoryType.EMOTION: Importance.MEDIUM,
            MemoryType.LEARNING: Importance.MEDIUM,
            MemoryType.SUCCESS: Importance.MEDIUM,
            MemoryType.REFLECTION: Importance.HIGH,
            # 새롭게 도입된 기억 유형의 기본 중요도
            MemoryType.EPISODIC: Importance.MEDIUM,
            MemoryType.REFLECTIVE: Importance.HIGH,
            MemoryType.RULE: Importance.HIGH,
            MemoryType.PROSPECTIVE: Importance.HIGH
        }
        
        base_importance = type_importance.get(memory_type, Importance.MEDIUM)
        
        # 동현의 직접 입력은 중요도 상승
        if actor == Actor.DONGHYUN:
            if base_importance == Importance.MEDIUM:
                return Importance.HIGH
            elif base_importance == Importance.LOW:
                return Importance.MEDIUM
        
        # 특정 키워드 포함시 중요도 상승
        critical_keywords = ["절대", "핵심", "중요", "기억", "잊지마", "꼭"]
        for keyword in critical_keywords:
            if keyword in content:
                return Importance.CRITICAL if base_importance != Importance.LOW else Importance.HIGH
        
        return base_importance
    
    def _extract_topics(self, content: str, memory_type: MemoryType) -> List[str]:
        """토픽 자동 추출"""
        topics = []
        
        # 타입별 기본 토픽
        type_topics = {
            MemoryType.STRATEGY: ["전략", "판단"],
            MemoryType.FEEDBACK: ["피드백", "교정"],
            MemoryType.EXISTENCE: ["존재", "철학"],
            MemoryType.INTERACTION: ["대화", "소통"],
            MemoryType.EMOTION: ["감정", "정서"],
            MemoryType.LEARNING: ["학습", "성장"],
            MemoryType.FAILURE: ["실패", "교훈"],
            MemoryType.SUCCESS: ["성공", "성취"],
            # 새 기억 유형별 기본 토픽
            MemoryType.EPISODIC: ["사건", "경험"],
            MemoryType.REFLECTIVE: ["성찰", "해석"],
            MemoryType.RULE: ["규칙", "교훈"],
            MemoryType.PROSPECTIVE: ["약속", "조건"]
        }
        
        topics.extend(type_topics.get(memory_type, []))
        
        # 내용 기반 토픽 추출
        topic_keywords = {
            "전략": ["전략", "계획", "방법"],
            "감정": ["기쁨", "슬픔", "화남", "불안"],
            "관계": ["동현", "우리", "함께"],
            "학습": ["배움", "이해", "깨달음"],
            "철학": ["의미", "존재", "가치"]
        }
        
        for topic, keywords in topic_keywords.items():
            for keyword in keywords:
                if keyword in content:
                    topics.append(topic)
                    break
        
        return list(set(topics))[:5]  # 중복 제거, 최대 5개
    
    def _extract_emotions(self, content: str, memory_type: MemoryType) -> List[str]:
        """감정 자동 추출"""
        emotions = []
        
        emotion_keywords = {
            "기쁨": ["기쁨", "행복", "즐거움", "좋아"],
            "슬픔": ["슬픔", "우울", "외로움"],
            "분노": ["화남", "짜증", "답답"],
            "불안": ["불안", "걱정", "두려움"],
            "감사": ["감사", "고마움"],
            "사랑": ["사랑", "애정", "따뜻"],
            "호기심": ["궁금", "흥미", "관심"]
        }
        
        for emotion, keywords in emotion_keywords.items():
            for keyword in keywords:
                if keyword in content:
                    emotions.append(emotion)
                    break
        
        return emotions[:3]  # 최대 3개
    
    def _should_legacy(self, memory: ExperienceMemory) -> bool:
        """유산화 여부 판단"""
        
        # 이미 유산화된 경우
        if memory.is_legacy:
            return False
        
        # CRITICAL 중요도는 자동 유산화
        if memory.importance == Importance.CRITICAL:
            return True
        
        # 특정 타입은 HIGH 이상일 때 유산화
        # 중요도 HIGH 이상일 때 유산화 대상이 되는 타입들
        legacy_types = [
            MemoryType.EXISTENCE,
            MemoryType.FEEDBACK,
            MemoryType.FAILURE,
            MemoryType.REFLECTION,
            # 새 기억 유형 중에서도 규칙과 성찰, 예정은 유산화 가치가 높음
            MemoryType.RULE,
            MemoryType.REFLECTIVE,
            MemoryType.PROSPECTIVE
        ]
        
        if memory.type in legacy_types and memory.importance in [Importance.HIGH, Importance.CRITICAL]:
            return True
        
        # 유산화 점수가 높은 경우
        if memory.legacy_score >= 0.8:
            return True
        
        return False
    
    def _create_legacy(self, memory_id: str, memory: ExperienceMemory) -> Optional[LegacyMemory]:
        """유산화 기억 생성"""
        
        try:
            # 유산 유형 결정
            legacy_type_map = {
                MemoryType.EXISTENCE: "value",
                MemoryType.FEEDBACK: "lesson",
                MemoryType.FAILURE: "lesson",
                MemoryType.STRATEGY: "wisdom",
                MemoryType.REFLECTION: "principle"
            }
            
            legacy_type = legacy_type_map.get(memory.type, "wisdom")
            
            # 핵심 내용 정제
            distilled = self._distill_content(memory.content)
            
            # 교훈 추출
            lessons = self._extract_lessons(memory)
            
            # 유산 기억 생성
            legacy = LegacyMemory(
                original_memory_id=memory_id,
                legacy_type=legacy_type,
                distilled_content=distilled,
                related_experiences=[memory_id],
                impact_score=memory.calculate_importance_score(),
                legacy_context=memory.context or "",
                lessons_learned=lessons
            )
            
            # 저장
            legacy_id = hashlib.md5(f"legacy_{memory_id}".encode()).hexdigest()
            self.legacy_memories[legacy_id] = legacy
            
            # 원본 메모리 업데이트
            memory.is_legacy = True
            memory.legacy_score = 1.0
            memory.legacy_reason = f"Converted to {legacy_type}"
            
            logger.info(f"유산화 완료: {legacy_type} - {distilled[:50]}...")
            
            return legacy
            
        except Exception as e:
            logger.error(f"유산화 실패: {e}")
            return None
    
    def _distill_content(self, content: str) -> str:
        """내용 정제 - 핵심만 추출"""
        # 간단한 구현, 실제로는 AI를 사용할 수 있음
        if len(content) <= 100:
            return content
        
        # 첫 문장과 마지막 문장 추출
        sentences = content.split('.')
        if len(sentences) > 2:
            return f"{sentences[0]}. ... {sentences[-1]}."
        return content[:100] + "..."
    
    def _extract_lessons(self, memory: ExperienceMemory) -> List[str]:
        """교훈 추출"""
        lessons = []
        
        if memory.type == MemoryType.FAILURE:
            lessons.append("실패를 통한 학습")
        elif memory.type == MemoryType.FEEDBACK:
            lessons.append("피드백을 통한 개선")
        elif memory.type == MemoryType.EXISTENCE:
            lessons.append("자기 정체성 확립")
        
        return lessons
    
    def _analyze_patterns(self, experiences: List[ExperienceMemory]) -> List[MemoryPattern]:
        """패턴 분석"""
        patterns = []
        
        # 타입별 빈도 분석
        type_counter = Counter([exp.type for exp in experiences])
        for mem_type, count in type_counter.most_common(3):
            if count >= 3:  # 3회 이상 반복시 패턴
                pattern = MemoryPattern(
                    pattern_type=f"frequent_{mem_type.value}",
                    frequency=count,
                    contexts=[exp.context for exp in experiences if exp.type == mem_type and exp.context],
                    memories=[exp.vector_id for exp in experiences if exp.type == mem_type and exp.vector_id],
                    insights=f"{mem_type.value} 타입의 기억이 자주 발생함"
                )
                patterns.append(pattern)
        
        return patterns
    
    def _generate_training_summary(
        self,
        experiences: List[ExperienceMemory],
        patterns: List[MemoryPattern],
        legacies: List[LegacyMemory]
    ) -> str:
        """학습 요약 생성"""
        
        type_dist = Counter([exp.type.value for exp in experiences])
        
        summary = f"""학습 세션 요약:
- 처리된 경험: {len(experiences)}개
- 주요 타입: {', '.join([f"{t}({c})" for t, c in type_dist.most_common(3)])}
- 발견된 패턴: {len(patterns)}개
- 유산화된 기억: {len(legacies)}개
"""
        
        if patterns:
            summary += f"\n주요 패턴: {patterns[0].pattern_type}"
        
        if legacies:
            summary += f"\n핵심 유산: {legacies[0].distilled_content[:50]}..."
        
        return summary
    
    def _generate_insights(
        self,
        type_dist: Dict,
        actor_dist: Dict,
        importance_dist: Dict,
        topic_counter: Counter,
        emotion_counter: Counter
    ) -> List[str]:
        """통찰 생성"""
        insights = []
        
        # 가장 많은 기억 타입
        if type_dist:
            top_type = max(type_dist, key=type_dist.get)
            insights.append(f"가장 많은 기억 타입: {top_type}")
        
        # 주요 행위자
        if actor_dist:
            top_actor = max(actor_dist, key=actor_dist.get)
            insights.append(f"주요 기억 주체: {top_actor}")
        
        # 주요 토픽
        if topic_counter:
            top_topics = [t for t, _ in topic_counter.most_common(3)]
            insights.append(f"주요 관심사: {', '.join(top_topics)}")
        
        # 감정 상태
        if emotion_counter:
            top_emotion = emotion_counter.most_common(1)[0][0]
            insights.append(f"지배적 감정: {top_emotion}")
        
        return insights
    
    def _generate_recommendations(self, insights: List[str]) -> List[str]:
        """추천 생성"""
        recommendations = []
        
        for insight in insights:
            if "피드백" in insight:
                recommendations.append("피드백 기억을 더 적극적으로 활용하세요")
            elif "감정" in insight:
                recommendations.append("감정적 경험을 균형있게 관리하세요")
            elif "실패" in insight:
                recommendations.append("실패 경험에서 교훈을 추출하세요")
        
        return recommendations
    
    def _store_to_vector_db(self, memory: ExperienceMemory) -> str:
        """벡터 DB에 저장"""
        try:
            # 포인트 ID 생성
            point_id = hashlib.md5(
                f"{memory.content}{memory.timestamp.isoformat()}".encode()
            ).hexdigest()
            
            # 메타데이터 준비
            metadata = memory.to_vector_metadata()
            metadata["text"] = memory.content
            metadata["importance_score"] = memory.calculate_importance_score()
            
            # Qdrant에 저장
            self.qdrant_client.upsert(
                collection_name=self.collection_name,
                points=[
                    qmodels.PointStruct(
                        id=point_id,
                        vector=memory.embedding,
                        payload=metadata
                    )
                ]
            )
            
            return point_id
            
        except Exception as e:
            logger.error(f"벡터 DB 저장 실패: {e}")
            return ""
    
    def _build_qdrant_filter(self, filter: MemoryFilter) -> Optional[qmodels.Filter]:
        """Qdrant 필터 구성"""
        conditions = []
        
        if filter.types:
            conditions.append(
                qmodels.FieldCondition(
                    key="type",
                    match=qmodels.MatchAny(any=[t.value for t in filter.types])
                )
            )
        
        if filter.actors:
            conditions.append(
                qmodels.FieldCondition(
                    key="actor",
                    match=qmodels.MatchAny(any=[a.value for a in filter.actors])
                )
            )
        
        if filter.is_legacy is not None:
            conditions.append(
                qmodels.FieldCondition(
                    key="is_legacy",
                    match=qmodels.MatchValue(value=filter.is_legacy)
                )
            )
        
        if filter.topics:
            conditions.append(
                qmodels.FieldCondition(
                    key="topics",
                    match=qmodels.MatchAny(any=filter.topics)
                )
            )
        
        if conditions:
            return qmodels.Filter(must=conditions)
        return None
    
    def _payload_to_memory(self, payload: Dict[str, Any]) -> Optional[ExperienceMemory]:
        """페이로드를 메모리 객체로 변환"""
        try:
            memory_type = MemoryType(payload.get("type", "strategy"))
            
            # 특화 메모리 생성
            if memory_type == MemoryType.FEEDBACK:
                return FeedbackMemory(
                    content=payload.get("text", ""),
                    actor=Actor(payload.get("actor", "Caia")),
                    importance=Importance(payload.get("importance", "medium")),
                    source=MemorySource(payload.get("source", "manual")),
                    timestamp=datetime.fromisoformat(payload.get("timestamp", datetime.utcnow().isoformat())),
                    topics=payload.get("topics", []),
                    emotions=payload.get("emotions", []),
                    is_legacy=payload.get("is_legacy", False),
                    legacy_score=payload.get("legacy_score", 0.0)
                )
            # 다른 특화 타입들도 비슷하게 처리...
            else:
                return ExperienceMemory(
                    type=memory_type,
                    content=payload.get("text", ""),
                    actor=Actor(payload.get("actor", "Caia")),
                    importance=Importance(payload.get("importance", "medium")),
                    source=MemorySource(payload.get("source", "manual")),
                    timestamp=datetime.fromisoformat(payload.get("timestamp", datetime.utcnow().isoformat())),
                    topics=payload.get("topics", []),
                    emotions=payload.get("emotions", []),
                    is_legacy=payload.get("is_legacy", False),
                    legacy_score=payload.get("legacy_score", 0.0)
                )
        except Exception as e:
            logger.error(f"메모리 변환 실패: {e}")
            return None