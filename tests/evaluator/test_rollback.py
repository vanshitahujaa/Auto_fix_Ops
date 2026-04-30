from engine.patch_generator import PatchGenerator

def test_apply_rollback_memory():
    generator = PatchGenerator()
    manifest = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "target-app",
                            "resources": {
                                "limits": {
                                    "memory": "512Mi",
                                    "cpu": "1000m"
                                }
                            }
                        }
                    ]
                }
            }
        }
    }
    
    previous_values = {
        "memory_limit_target-app": "128Mi"
    }
    
    reverted = generator.apply_rollback(manifest, previous_values)
    limits = reverted["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]
    
    assert limits["memory"] == "128Mi"
    assert limits["cpu"] == "1000m"

def test_apply_rollback_restart():
    generator = PatchGenerator()
    manifest = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "kubectl.kubernetes.io/restartedAt": "2023-10-10T00:00:00Z"
                    }
                },
                "spec": {
                    "containers": [{"name": "app"}]
                }
            }
        }
    }
    
    previous_values = {
        "restart_annotation": "never"
    }
    
    reverted = generator.apply_rollback(manifest, previous_values)
    annotations = reverted["spec"]["template"]["metadata"].get("annotations", {})
    assert "kubectl.kubernetes.io/restartedAt" not in annotations
    
def test_apply_rollback_restart_previous():
    generator = PatchGenerator()
    manifest = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "kubectl.kubernetes.io/restartedAt": "2023-10-10T00:00:00Z"
                    }
                },
                "spec": {
                    "containers": [{"name": "app"}]
                }
            }
        }
    }
    
    previous_values = {
        "restart_annotation": "2023-09-09T00:00:00Z"
    }
    
    reverted = generator.apply_rollback(manifest, previous_values)
    annotations = reverted["spec"]["template"]["metadata"]["annotations"]
    assert annotations["kubectl.kubernetes.io/restartedAt"] == "2023-09-09T00:00:00Z"
