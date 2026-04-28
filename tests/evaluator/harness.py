import json
import logging
import os
from engine.summarizer import ContextSummarizer
from engine.baseline import RuleBasedDiagnosisEngine
from engine.ai_diagnostics import AIDiagnosisEngine
from engine.memory import QdrantMemoryStore

logging.basicConfig(level=logging.ERROR) # Suppress noise for harness metrics

def run_evaluation():
    print("\n🚀 Starting AutoFixOps V1 System Validation...\n")
    
    with open("tests/evaluator/dataset.json", "r") as f:
        dataset = json.load(f)
        
    rule_engine = RuleBasedDiagnosisEngine()
    
    # We gracefully skip AI instantiation if OPENAI_API_KEY is missing during local evaluation
    if os.getenv("OPENAI_API_KEY"):
        ai_engine = AIDiagnosisEngine()
        qdrant = QdrantMemoryStore()
    else:
        ai_engine = None
        qdrant = None
        print("⚠️ OPENAI_API_KEY not found. Simulating AI Fallback block.")
        
    metrics = {
        "total": len(dataset),
        "rule_hits": 0,
        "ai_hits": 0,
        "ai_fallback": 0,
        "correct": 0,
        "errors": 0
    }
    
    print("-" * 60)
    print(f"{'Incident ID':<15} | {'Expected Engine':<10} | {'Actual Engine':<10} | {'Status'}")
    print("-" * 60)
    
    for incident in dataset:
        context_doc = {"metrics": incident["mongo_metrics"]}
        alert_name = incident["alert_name"]
        
        # 1. Pipeline Start -> Rule Engine
        verdict = rule_engine.analyze(alert_name, context_doc)
        actual_engine = "RULE"
        final_diagnosis = verdict.get("root_cause_classification")
        
        # 2. Pipeline Fallback -> AI Engine
        if final_diagnosis == "UNKNOWN_ANOMALY":
            summary = ContextSummarizer.summarize(context_doc)
            actual_engine = "AI"
            
            if ai_engine:
                rag_hits = qdrant.retrieve_similar(summary.model_dump_json())
                ai_verdict = ai_engine.analyze(summary, rag_hits)
                final_diagnosis = ai_verdict.get("diagnosis", "UNKNOWN")
            else:
                # Simulating fallback
                final_diagnosis = "UNKNOWN"
                metrics["ai_fallback"] += 1
                
            metrics["ai_hits"] += 1
        else:
            metrics["rule_hits"] += 1
            
        # Evaluation Logic
        expected_diagnosis = incident["expected_diagnosis"]
        expected_engine = incident["expected_engine"]
        
        match = (final_diagnosis == expected_diagnosis) and (actual_engine == expected_engine)
        
        if match:
            metrics["correct"] += 1
            status = "✅ PASS"
        else:
            metrics["errors"] += 1
            status = f"❌ FAIL (Got: {final_diagnosis})"
            
        print(f"{incident['id']:<15} | {expected_engine:<10} | {actual_engine:<10} | {status}")

    print("-" * 60)
    print("\n📊 Evaluation Complete")
    accuracy = (metrics["correct"] / metrics["total"]) * 100
    print(f"Total Processed: {metrics['total']}")
    print(f"Rule Engine Triage Rate: {(metrics['rule_hits'] / metrics['total']) * 100:.1f}%")
    if ai_engine:
        print(f"AI Safe Fallback Rate: {(metrics['ai_fallback'] / metrics['ai_hits']) * 100:.1f}%" if metrics['ai_hits'] > 0 else "N/A")
    print(f"End-to-End System Accuracy: {accuracy:.1f}%\n")

if __name__ == "__main__":
    run_evaluation()
