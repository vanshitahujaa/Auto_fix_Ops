from fastapi import FastAPI
import asyncio

app = FastAPI(title="AutoFixOps Target App")

# Global variables for chaos memory
data = []
cpu_stress_flag = False

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/leak")
def leak():
    """
    Simulates a memory leak by continuously appending data to a global list.
    Each call adds ~10MB of string data.
    """
    global data
    data.append("x" * 10**7)
    return {"status": "leaking", "current_mb_leaked": len(data) * 10}

@app.get("/cpu")
def cpu():
    """
    Simulates a CPU spike by pinning a worker thread in an infinite loop.
    This will also block the health endpoint from responding if run synchronously,
    intentionally failing the liveness probe.
    """
    global cpu_stress_flag
    cpu_stress_flag = True
    while cpu_stress_flag:
        pass
    return {"status": "cpu_spiking"}

@app.get("/recover")
def recover():
    """
    Simulates successful recovery/state neutralization.
    """
    global data
    global cpu_stress_flag
    data = []
    cpu_stress_flag = False
    return {"status": "recovered"}
