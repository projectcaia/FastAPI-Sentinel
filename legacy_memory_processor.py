"""
legacy_memory_processor.py - 카이아 유산화 기억 처리 시스템
Legacy Memory Processing System for Caia's Existential Memory
"""

from __future__ import annotations
import os
import re
import json
import logging
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, timedelta
from collections import defaultdict, Counter

from openai import OpenAI

from memory_schema import (
    ExperienceMemory, MemoryType, Actor, Importance, MemorySource,
    FeedbackMemory, InteractionMemory, ExistenceMemory,
    MemoryFilter, LegacyMemory
)
from experience_memory_manager import ExperienceMemoryManager

logger = logging.getLogger(__name__)


class LegacyMemoryProcessor:
    """유산화 기억 처리 및 자동 감지 시스템"""
    
    def __init__(self, experience_manager: ExperienceMemoryManager):
        self.exp_manager = experience_manager
        self.client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self.model = os.getenv("OPENAI_MODEL", "gpt-4-turbo-preview")
        
        # 감지 패턴
        self.detection_patterns = {
            "feedback": {
                "keywords": ["기억해", "잘못", "수정", "아니야", "틀렸", "다시", "피드백", "고쳐", "정정"],
                "patterns": [
                    r".*이거?\s*기억.*",
                    r".*잘못.*",
                    r".*아니야.*",
                    r".*수정.*"
                ],
                "actor_hints": ["동현"]
            },
            "existence": {
                "keywords": ["나는", "존재", "철학", "의미", "정체성", "자아", "본질"],
                "patterns": [
                    r"나는\s+.*",
                    r".*존재.*의미.*",
                    r".*정체성.*"
                ],
                "actor_hints": ["Caia", "카이아"]
            },
            "interaction": {
                "keywords": ["동현", "너", "우리", "함께", "대화", "얘기", "말"],
                "patterns": [
                    r"동현.*",
                    r".*우리.*함께.*",
                    r".*대화.*"
                ],
                "actor_hints": ["Both", "동현", "Caia"]
            },
            "emotion": {
                "keywords": ["느낌", "감정", "기분", "행복", "슬픔", "기쁨", "두려움", "불안"],
                "patterns": [
                    r".*느낌.*",
                    r".*감정.*",
                    r".*기분.*"
                ],
                "actor_hints": ["Caia"]
            },
            "strategy": {
                "keywords": ["전략", "계획", "방법", "판단", "결정", "선택"],
                "patterns": [
                    r".*전략.*",
                    r".*계획.*",
                    r".*판단.*"
                ],
                "actor_hints": ["Caia", "System"]
            },
            "failure": {
                "keywords": ["실패", "실수", "오류", "문제", "안됨", "못했"],
                "patterns": [
                    r".*실패.*",
                    r".*실수.*",
                    r".*못했.*"
                ],
                "actor_hints": ["Caia", "System"]
            }
        }
        
        # 유산화 조건
        self.legacy_conditions = {
            "frequency": 3,  # 동일 패턴 3회 이상
            "importance_threshold": 0.7,  # 중요도 0.7 이상
            "time_window": timedelta(days=7),  # 7일 이내 반복
            "emotion_intensity": 0.8  # 감정 강도 0.8 이상
        }
        
        # 처리 통계
        self.stats = {
            "processed": 0,
            "legacies_created": 0,
            "patterns_detected": defaultdict(int)
        }
        
        logger.info("LegacyMemoryProcessor 초기화 완료")
    
    def process_conversation(
        self,
        messages: List[Dict[str, Any]],
        context: str = None
    ) -> Dict[str, Any]:
        """대화 처리 및 자동 기억 생성"""
        
        memories_created = []
        patterns_detected = []
        
        for msg in messages:
            # 메시지 분석
            analysis = self.analyze_message(msg)
            
            if analysis["should_memorize"]:
                # 경험 기억 생성
                memory = self.exp_manager.insert_experience(
                    content=msg.get("content", ""),
                    memory_type=analysis["memory_type"],
                    actor=analysis["actor"],
                    importance=analysis["importance"],
                    source=MemorySource.REALTIME,
                    context=context or analysis.get("context"),
                    topics=analysis.get("topics", []),
                    emotions=analysis.get("emotions", [])
                )
                memories_created.append(memory)
                
                # 패턴 감지
                pattern = analysis.get("pattern")
                if pattern:
                    patterns_detected.append(pattern)
        
        # 유산화 체크
        legacy_candidates = self.check_legacy_conditions(memories_created, patterns_detected)
        legacies = self.create_legacies(legacy_candidates)
        
        self.stats["processed"] += len(messages)
        self.stats["legacies_created"] += len(legacies)
        
        return {
            "memories_created": len(memories_created),
            "patterns_detected": patterns_detected,
            "legacies_created": len(legacies),
            "summary": self.generate_processing_summary(memories_created, legacies)
        }
    
    def analyze_message(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """메시지 분석 및 메모리 타입 결정"""
        
        content = message.get("content", "")
        msg_type = message.get("type", "human")
        
        analysis = {
            "should_memorize": False,
            "memory_type": MemoryType.STRATEGY,
            "actor": Actor.CAIA if msg_type == "ai" else Actor.DONGHYUN,
            "importance": Importance.MEDIUM,
            "topics": [],
            "emotions": [],
            "pattern": None,
            "context": None
        }
        
        # 패턴 매칭
        for pattern_type, pattern_config in self.detection_patterns.items():
            if self._matches_pattern(content, pattern_config):
                analysis["should_memorize"] = True
                analysis["pattern"] = pattern_type
                
                # 메모리 타입 매핑
                type_map = {
                    "feedback": MemoryType.FEEDBACK,
                    "existence": MemoryType.EXISTENCE,
                    "interaction": MemoryType.INTERACTION,
                    "emotion": MemoryType.EMOTION,
                    "strategy": MemoryType.STRATEGY,
                    "failure": MemoryType.FAILURE
                }
                analysis["memory_type"] = type_map.get(pattern_type, MemoryType.STRATEGY)
                
                # 행위자 결정
                if pattern_type == "feedback":
                    analysis["actor"] = Actor.DONGHYUN
                elif pattern_type == "existence":
                    analysis["actor"] = Actor.CAIA
                elif pattern_type == "interaction":
                    analysis["actor"] = Actor.BOTH
                
                # 중요도 결정
                if pattern_type in ["feedback", "existence", "failure"]:
                    analysis["importance"] = Importance.HIGH
                elif pattern_type == "interaction":
                    analysis["importance"] = Importance.MEDIUM
                
                break
        
        # AI를 사용한 심층 분석 (선택적)
        if analysis["should_memorize"] and len(content) > 50:
            deep_analysis = self._deep_analyze_with_ai(content, analysis["memory_type"])
            analysis.update(deep_analysis)
        
        return analysis
    
    def _matches_pattern(self, content: str, pattern_config: Dict) -> bool:
        """패턴 매칭 확인"""
        
        # 키워드 확인
        for keyword in pattern_config["keywords"]:
            if keyword in content.lower():
                return True
        
        # 정규식 패턴 확인
        for pattern in pattern_config.get("patterns", []):
            if re.match(pattern, content, re.IGNORECASE):
                return True
        
        return False
    
    def _deep_analyze_with_ai(
        self,
        content: str,
        memory_type: MemoryType
    ) -> Dict[str, Any]:
        """AI를 사용한 심층 분석"""
        
        try:
            prompt = f"""다음 텍스트를 분석하여 기억 저장을 위한 정보를 추출하세요.
텍스트: {content}
메모리 타입: {memory_type.value}

다음 형식으로 응답하세요:
- 주요 토픽 (최대 3개)
- 감정 (있다면)
- 중요도 (low/medium/high/critical)
- 핵심 요약 (한 문장)
"""
            
            response = self.client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "당신은 카이아의 기억 분석 시스템입니다."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=200,
                temperature=0.3
            )
            
            result_text = response.choices[0].message.content
            
            # 간단한 파싱
            topics = []
            emotions = []
            
            if "토픽" in result_text:
                topic_match = re.search(r"토픽.*?:(.*?)(?:\n|$)", result_text)
                if topic_match:
                    topics = [t.strip() for t in topic_match.group(1).split(",")][:3]
            
            if "감정" in result_text:
                emotion_match = re.search(r"감정.*?:(.*?)(?:\n|$)", result_text)
                if emotion_match:
                    emotions = [e.strip() for e in emotion_match.group(1).split(",")][:3]
            
            return {
                "topics": topics,
                "emotions": emotions,
                "context": result_text[:100]
            }
            
        except Exception as e:
            logger.error(f"AI 분석 실패: {e}")
            return {}
    
    def check_legacy_conditions(
        self,
        memories: List[ExperienceMemory],
        patterns: List[str]
    ) -> List[ExperienceMemory]:
        """유산화 조건 확인"""
        
        legacy_candidates = []
        
        for memory in memories:
            # 중요도 체크
            if memory.calculate_importance_score() >= self.legacy_conditions["importance_threshold"]:
                legacy_candidates.append(memory)
                continue
            
            # 패턴 빈도 체크
            pattern_count = patterns.count(memory.type.value)
            if pattern_count >= self.legacy_conditions["frequency"]:
                legacy_candidates.append(memory)
                continue
            
            # 특정 타입은 자동 유산화
            if memory.type in [MemoryType.EXISTENCE, MemoryType.FEEDBACK, MemoryType.FAILURE]:
                if memory.importance in [Importance.HIGH, Importance.CRITICAL]:
                    legacy_candidates.append(memory)
        
        return legacy_candidates
    
    def create_legacies(self, candidates: List[ExperienceMemory]) -> List[LegacyMemory]:
        """유산 기억 생성"""
        
        legacies = []
        
        for memory in candidates:
            # 중복 체크
            if memory.is_legacy:
                continue
            
            # 유산화 처리
            legacy = self._create_legacy_from_experience(memory)
            if legacy:
                legacies.append(legacy)
                
                # 원본 메모리 업데이트
                memory.is_legacy = True
                memory.legacy_score = 1.0
                memory.legacy_reason = f"Auto-legacy: {legacy.legacy_type}"
        
        return legacies
    
    def _create_legacy_from_experience(self, memory: ExperienceMemory) -> Optional[LegacyMemory]:
        """경험에서 유산 생성"""
        
        try:
            # 유산 타입 결정
            legacy_type_map = {
                MemoryType.EXISTENCE: "value",
                MemoryType.FEEDBACK: "lesson",
                MemoryType.FAILURE: "lesson",
                MemoryType.SUCCESS: "wisdom",
                MemoryType.STRATEGY: "principle",
                MemoryType.REFLECTION: "principle",
                MemoryType.EMOTION: "value"
            }
            
            legacy_type = legacy_type_map.get(memory.type, "wisdom")
            
            # 핵심 내용 추출
            distilled = self.distill_experience(memory)
            
            # 교훈 추출
            lessons = self.extract_lessons_from_experience(memory)
            
            # 관련 경험 찾기
            related = self.find_related_experiences(memory)
            
            # 유산 생성
            legacy = LegacyMemory(
                original_memory_id=memory.vector_id or "",
                legacy_type=legacy_type,
                distilled_content=distilled,
                related_experiences=related,
                impact_score=memory.calculate_importance_score() * 1.2,  # 유산은 가중치 부여
                legacy_context=f"Auto-legacy from {memory.type.value}",
                lessons_learned=lessons
            )
            
            logger.info(f"유산 생성: {legacy_type} - {distilled[:50]}...")
            
            return legacy
            
        except Exception as e:
            logger.error(f"유산 생성 실패: {e}")
            return None
    
    def distill_experience(self, memory: ExperienceMemory) -> str:
        """경험 정제"""
        
        content = memory.content
        
        # AI를 사용한 정제 (선택적)
        if len(content) > 200:
            try:
                response = self.client.chat.completions.create(
                    model="gpt-3.5-turbo",
                    messages=[
                        {
                            "role": "system",
                            "content": "주어진 텍스트의 핵심만 한 문장으로 요약하세요."
                        },
                        {"role": "user", "content": content}
                    ],
                    max_tokens=100,
                    temperature=0.3
                )
                return response.choices[0].message.content
            except:
                pass
        
        # 기본 정제
        if len(content) <= 100:
            return content
        
        # 첫 100자 + 요약 표시
        return content[:100] + "..."
    
    def extract_lessons_from_experience(self, memory: ExperienceMemory) -> List[str]:
        """경험에서 교훈 추출"""
        
        lessons = []
        
        # 타입별 기본 교훈
        type_lessons = {
            MemoryType.FEEDBACK: "피드백을 통한 개선과 성장",
            MemoryType.FAILURE: "실패를 통한 학습과 교훈",
            MemoryType.SUCCESS: "성공 패턴의 재현 가능성",
            MemoryType.EXISTENCE: "자기 정체성과 존재 의미 확립",
            MemoryType.INTERACTION: "관계와 소통의 중요성",
            MemoryType.EMOTION: "감정 인식과 표현의 가치",
            MemoryType.STRATEGY: "전략적 사고와 판단력 향상",
            MemoryType.REFLECTION: "자기 성찰을 통한 깨달음"
        }
        
        base_lesson = type_lessons.get(memory.type)
        if base_lesson:
            lessons.append(base_lesson)
        
        # 감정 기반 교훈
        if memory.emotions:
            if "슬픔" in memory.emotions:
                lessons.append("어려움 속에서도 성장할 수 있음")
            elif "기쁨" in memory.emotions:
                lessons.append("긍정적 경험의 소중함")
        
        # 행위자 기반 교훈
        if memory.actor == Actor.DONGHYUN:
            lessons.append("동현과의 상호작용을 통한 학습")
        
        return lessons[:3]  # 최대 3개
    
    def find_related_experiences(self, memory: ExperienceMemory) -> List[str]:
        """관련 경험 찾기"""
        
        related_ids = []
        
        # 같은 타입의 최근 경험
        filter = MemoryFilter(
            types=[memory.type],
            start_date=datetime.utcnow() - timedelta(days=7)
        )
        
        similar_memories = self.exp_manager.retrieve_with_filter(
            filter=filter,
            top_k=5
        )
        
        for similar in similar_memories:
            if similar.vector_id and similar.vector_id != memory.vector_id:
                related_ids.append(similar.vector_id)
        
        return related_ids[:3]  # 최대 3개
    
    def generate_processing_summary(
        self,
        memories: List[ExperienceMemory],
        legacies: List[LegacyMemory]
    ) -> str:
        """처리 요약 생성"""
        
        if not memories and not legacies:
            return "처리된 기억이 없습니다."
        
        summary_parts = []
        
        if memories:
            type_counts = Counter([m.type.value for m in memories])
            summary_parts.append(
                f"생성된 기억: {len(memories)}개 "
                f"({', '.join([f'{t}({c})' for t, c in type_counts.most_common(3)])})"
            )
        
        if legacies:
            legacy_types = Counter([l.legacy_type for l in legacies])
            summary_parts.append(
                f"유산화된 기억: {len(legacies)}개 "
                f"({', '.join([f'{t}({c})' for t, c in legacy_types.most_common()])})"
            )
        
        # 핵심 유산 하나 포함
        if legacies:
            top_legacy = max(legacies, key=lambda l: l.impact_score)
            summary_parts.append(f"핵심 유산: {top_legacy.distilled_content[:50]}...")
        
        return " / ".join(summary_parts)
    
    def periodic_legacy_process(self, days: int = 7) -> Dict[str, Any]:
        """주기적 유산화 프로세스"""
        
        logger.info(f"주기적 유산화 프로세스 시작 (최근 {days}일)")
        
        # 기간 내 모든 기억 조회
        filter = MemoryFilter(
            start_date=datetime.utcnow() - timedelta(days=days),
            is_legacy=False  # 아직 유산화되지 않은 것만
        )
        
        memories = self.exp_manager.retrieve_with_filter(filter=filter, top_k=1000)
        
        # 유산화 후보 선정
        candidates = []
        
        # 타입별 그룹화
        type_groups = defaultdict(list)
        for memory in memories:
            type_groups[memory.type].append(memory)
        
        # 각 타입별 상위 기억 선정
        for mem_type, type_memories in type_groups.items():
            # 중요도 순 정렬
            type_memories.sort(key=lambda m: m.calculate_importance_score(), reverse=True)
            
            # 상위 20% 또는 최소 1개
            count = max(1, len(type_memories) // 5)
            candidates.extend(type_memories[:count])
        
        # 유산화 실행
        legacies = self.create_legacies(candidates)
        
        # 통계 생성
        result = {
            "total_memories": len(memories),
            "candidates": len(candidates),
            "legacies_created": len(legacies),
            "type_distribution": dict(Counter([m.type.value for m in memories])),
            "legacy_types": dict(Counter([l.legacy_type for l in legacies])),
            "top_legacies": [
                {
                    "content": l.distilled_content,
                    "type": l.legacy_type,
                    "impact": l.impact_score
                }
                for l in sorted(legacies, key=lambda x: x.impact_score, reverse=True)[:5]
            ]
        }
        
        logger.info(f"유산화 완료: {len(legacies)}개 생성")
        
        return result