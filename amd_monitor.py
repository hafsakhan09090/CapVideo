"""
AMD System Monitor for CapVideo
Safe imports with complete isolation
"""
import threading
import time
from typing import Dict, Any
from datetime import datetime

# Import the accelerator (which has safe isolation)
try:
    from amd_accelerator import AMDAccelerator
except ImportError:
    # Fallback for standalone
    import sys
    import os
    sys.path.append(os.path.dirname(__file__))
    from amd_accelerator import AMDAccelerator

class AMDMonitor:
    """Real-time AMD hardware monitor with complete isolation"""
    
    def __init__(self, update_interval=2):
        self.accelerator = AMDAccelerator()
        self.update_interval = update_interval
        self.metrics_history = []
        self.max_history = 60
        self.running = False
        self.monitor_thread = None
        
    def start_monitoring(self):
        """Start background monitoring thread"""
        if self.running:
            return
        
        self.running = True
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()
        
    def stop_monitoring(self):
        """Stop monitoring thread"""
        self.running = False
        if self.monitor_thread:
            self.monitor_thread.join(timeout=1)
            
    def _monitor_loop(self):
        """Background monitoring loop"""
        while self.running:
            try:
                metrics = self.accelerator.get_gpu_metrics()
                metrics['timestamp'] = datetime.now().isoformat()
                
                self.metrics_history.append(metrics)
                if len(self.metrics_history) > self.max_history:
                    self.metrics_history.pop(0)
                    
                time.sleep(self.update_interval)
            except Exception as e:
                # Don't crash on monitoring errors
                time.sleep(5)
    
    def get_current_metrics(self) -> Dict[str, Any]:
        """Get current AMD metrics with fallback"""
        try:
            return self.accelerator.get_gpu_metrics()
        except:
            return {
                'gpu_count': 0,
                'gpus': [],
                'error': 'Metrics temporarily unavailable'
            }
    
    def get_performance_summary(self) -> str:
        """Generate human-readable performance summary with fallback"""
        try:
            metrics = self.get_current_metrics()
            report = self.accelerator.get_system_report()
            
            if metrics.get('gpu_count', 0) > 0:
                lines = [
                    "🎯 AMD Performance Report",
                    "=" * 50,
                    f"ROCm Version: {report['amd_libraries'].get('rocm_version', 'N/A')}",
                    f"Optimization Level: {report.get('optimization_level', 'Basic')}",
                    f"AMD Libraries:",
                    f"  • AMD SMI: {'✅' if report['amd_libraries'].get('amdsmi') else '❌'}",
                    f"  • AOCL Optimized: {'✅' if report['amd_libraries'].get('aocl_optimized') else '❌'}",
                ]
                
                if metrics['gpus']:
                    for gpu in metrics['gpus']:
                        if 'error' not in gpu:
                            lines.append(f"\nGPU {gpu['id']}: {gpu['name']}")
                            lines.append(f"  Temperature: {gpu.get('temperature_c', 'N/A')}°C")
                            lines.append(f"  Utilization: {gpu.get('utilization_percent', 'N/A')}%")
                
                return "\n".join(lines)
            else:
                return "No AMD GPU detected - running in CPU mode"
                
        except Exception as e:
            return f"AMD monitoring unavailable: {str(e)}"
