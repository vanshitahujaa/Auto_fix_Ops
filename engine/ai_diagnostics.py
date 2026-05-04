import os
import logging
from typing import Dict, Any, List
from pydantic import BaseModel, Field
from langchain_core.prompts import PromptTemplate
from langchain_openai import ChatOpenAI
from langchain_core.output_parsers import PydanticOutputParser

from .summarizer import IncidentSummary

logger = logging.getLogger("autofixops")

class AIAction(BaseModel):
    type: str = Field(description="Must be strictly one of: ESCALATE, RESTART_POD, SCALE_UP")
    target: str = Field(description="The target resource")
    reasoning: List[str] = Field(description="Short factual bullet points for the decision.")

class AIFinalVerdict(BaseModel):
    diagnosis: str = Field(description="Must be strictly one of: CPU_SPIKE, MEMORY_LEAK, CRASH_LOOP, UNKNOWN")
    confidence: float = Field(description="Float between 0.0 and 1.0")
    recommended_action: AIAction

class AIDiagnosisEngine:
    """
    Phase 3 LLM Engine. Strictly bounded reasoning restricted to Pydantic outputs.
    """
    def __init__(self):
        # We assume OPENAI_API_KEY and OPENAI_API_BASE are available in the environment
        model_name = os.getenv("OPENAI_MODEL_NAME", "gpt-4o-mini")
        self.llm = ChatOpenAI(temperature=0.0, model=model_name)
        self.parser = PydanticOutputParser(pydantic_object=AIFinalVerdict)

        self.prompt = PromptTemplate(
            template="""You are the AutoFixOps Diagnosis Engine.
You receive a mathematical summary of a Kubernetes incident and historical context.
Do not invent facts. Classify the incident into a known category.

Incident Summary:
{summary_json}

Historical RAG Context (Past successful resolutions):
{rag_context}

Known Failed Approaches (DO NOT recommend these actions for similar incidents):
{failed_context}

{format_instructions}
""",
            input_variables=["summary_json", "rag_context", "failed_context"],
            partial_variables={"format_instructions": self.parser.get_format_instructions()},
        )

    def analyze(self, summary: IncidentSummary, rag_hits: list, failed_hits: list = None) -> Dict[str, Any]:
        logger.info("[AI ENGINE] Invoking deterministic LLM chain.")
        
        chain = self.prompt | self.llm | self.parser
        
        try:
            verdict: AIFinalVerdict = chain.invoke({
                "summary_json": summary.model_dump_json(),
                "rag_context": str(rag_hits) if rag_hits else "No history available.",
                "failed_context": str(failed_hits) if failed_hits else "No known failures."
            })
            
            # 1. Dual Validation / Guard: Confidence Threshold rejection
            if verdict.confidence < 0.75:
                logger.warning(f"[AI ENGINE] Rejected: Confidence {verdict.confidence} below 0.75 threshold.")
                return self._trigger_fallback("AI lacked confidence in its diagnosis.")
                
            # 2. Read-Only Guard
            if verdict.recommended_action.type != "ESCALATE":
                logger.warning(f"[AI ENGINE] Read-Only Guard: Coercing action {verdict.recommended_action.type} -> ESCALATE.")
                verdict.recommended_action.type = "ESCALATE"
                
            logger.info("[AI ENGINE] LLM successfully output bounded pydantic schema.")
            return verdict.model_dump()
            
        except Exception as e:
            logger.error(f"[AI ENGINE FAILED] LLM generated invalid output or crashed: {e}")
            return self._trigger_fallback(f"Pydantic validation or API error: {e}")

    def _trigger_fallback(self, reason: str):
        return {
            "diagnosis": "UNKNOWN",
            "confidence": 0.0,
            "recommended_action": {
                "type": "ESCALATE",
                "target": "human",
                "reasoning": [reason]
            }
        }
