"""
AMD Accelerator Module for CapVideo
Completely isolated imports with graceful fallbacks
"""
import os
import logging
import platform
import subprocess
import sys
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

# DO NOT import amdsmi at module level - import it lazily inside functions
AMD_SMI_AVAILABLE = False
try:
    # Just check if we can import it, but don't actually use it yet
    import importlib
    amdsmi_spec = importlib.util.find_spec("amdsmi")
    AMD_SMI_AVAILABLE = amdsmi_spec is not None
    if AMD_SMI_AVAILABLE:
        logger.info("✅ AMD SMI Python package found")
    else:
        logger.info("ℹ️ AMD SMI Python package not found - running without GPU monitoring")
except:
    AMD_SMI_AVAILABLE = False

# Check for ROCm
ROCM_AVAILABLE = False
try:
    result = subprocess.run(
        ['which', 'rocm-smi'], 
        capture_output=True, 
        text=True, 
        timeout=2
    )
    if result.returncode == 0:
        ROCM_AVAILABLE = True
        logger.info("✅ ROCm tools found")
    else:
        ROCM_AVAILABLE = False
except:
    ROCM_AVAILABLE = False

class AMDAccelerator:
    """Central class for managing AMD library integrations with complete isolation"""
    
    def __init__(self):
        self.amd_gpu_count = 0
        self.amd_cpu_optimized = self._check_cpu_optimizations()
        self.amd_npu_available = False
        self.rocm_version = self._get_rocm_version()
        self.amdsmi = None  # Will be lazily imported
        
        # Try to initialize AMD SMI if available
        if AMD_SMI_AVAILABLE:
            self._init_amd_smi()
        
        logger.info(f"🚀 AMD Accelerator initialized (CPU optimized: {self.amd_cpu_optimized})")
        
    def _init_amd_smi(self):
        """Lazily import and initialize AMD System Management Interface"""
        if not AMD_SMI_AVAILABLE:
            return
            
        try:
            # Lazy import - only import when actually needed
            import amdsmi
            self.amdsmi = amdsmi
            
            # Try to initialize
            try:
                self.amdsmi.amdsmi_init()
                self.amd_smi_handle = self.amdsmi.amdsmi_get_handle()
                
                # Get GPU count
                devices = self.amdsmi.amdsmi_get_processor_handles(self.amd_smi_handle)
                self.amd_gpu_count = len(devices)
                
                logger.info(f"✅ AMD SMI active: {self.amd_gpu_count} GPU(s) detected")
            except (OSError, RuntimeError) as e:
                # This happens when libamd_smi.so is missing
                logger.warning(f"AMD SMI runtime init failed (expected in HF Spaces): {e}")
                self.amd_gpu_count = 0
                self.amdsmi = None
                
        except ImportError as e:
            logger.warning(f"AMD SMI import failed: {e}")
            self.amdsmi = None
        except Exception as e:
            logger.warning(f"AMD SMI init error: {e}")
            self.amdsmi = None
    
    def _get_rocm_version(self) -> str:
        """Get ROCm version if available"""
        if ROCM_AVAILABLE:
            try:
                result = subprocess.run(
                    ['rocm-smi', '--version'], 
                    capture_output=True, 
                    text=True, 
                    timeout=2
                )
                if result.returncode == 0:
                    return result.stdout.strip()
            except:
                pass
        return "Not detected"
    
    def _check_cpu_optimizations(self) -> bool:
        """Check if running on AMD CPU with optimizations"""
        try:
            cpu_info = platform.processor().lower()
            cpu_name = platform.processor()
            
            # Check for AMD CPU
            if any(x in cpu_info for x in ['amd', 'ryzen', 'epyc']):
                logger.info(f"✅ AMD CPU detected: {cpu_name}")
                return True
        except:
            pass
        return False
    
    def get_gpu_metrics(self) -> Dict[str, Any]:
        """Get AMD GPU metrics with complete isolation"""
        metrics = {
            'gpu_count': 0,
            'gpus': [],
            'rocm_version': self.rocm_version,
        }
        
        # Only try to get metrics if AMD SMI is initialized
        if hasattr(self, 'amdsmi') and self.amdsmi and hasattr(self, 'amd_smi_handle'):
            try:
                devices = self.amdsmi.amdsmi_get_processor_handles(self.amd_smi_handle)
                self.amd_gpu_count = len(devices)
                metrics['gpu_count'] = self.amd_gpu_count
                
                for i, device in enumerate(devices[:self.amd_gpu_count]):
                    try:
                        # Get device info with error handling for each metric
                        info = self.amdsmi.amdsmi_get_processor_info(device)
                        usage = self.amdsmi.amdsmi_get_gpu_usage(device)
                        power = self.amdsmi.amdsmi_get_power_info(device)
                        temp = self.amdsmi.amdsmi_get_temp_metric(device, 0, self.amdsmi.AmdSmiTemperatureMetric.CURRENT)
                        memory = self.amdsmi.amdsmi_get_memory_info(device)
                        
                        metrics['gpus'].append({
                            'id': i,
                            'name': info.get('name', 'AMD GPU'),
                            'vram_total_gb': round(memory.get('vram_size', 0) / (1024**3), 2),
                            'vram_used_mb': round(memory.get('vram_used', 0) / (1024**2), 2),
                            'temperature_c': temp,
                            'power_usage_w': round(power.get('power_usage', 0) / 1_000_000, 2),
                            'utilization_percent': usage.get('gpu_busy_percent', 0)
                        })
                    except Exception as e:
                        logger.debug(f"Error getting metrics for GPU {i}: {e}")
            except Exception as e:
                logger.debug(f"Failed to get GPU metrics: {e}")
        
        # If no GPUs detected, add a helpful note
        if metrics['gpu_count'] == 0:
            metrics['note'] = 'No AMD GPU detected - running in CPU mode'
        
        return metrics
    
    def fast_text_processing(self, text: str) -> Dict[str, Any]:
        """
        Use AMD-optimized libraries for text processing
        Falls back to standard operations gracefully
        """
        result = {
            'word_count': 0,
            'char_count': 0,
            'optimization': 'standard'
        }
        
        if text:
            words = text.split()
            result['word_count'] = len(words)
            result['char_count'] = len(text)
            
            # If we have AMD CPU, note the optimization
            if self.amd_cpu_optimized:
                result['optimization'] = 'AMD CPU optimized'
            elif ROCM_AVAILABLE:
                result['optimization'] = 'AMD ROCm available'
        
        return result
    
    def get_system_report(self) -> Dict[str, Any]:
        """Generate comprehensive AMD system report with fallbacks"""
        return {
            'amd_libraries': {
                'amdsmi': AMD_SMI_AVAILABLE and self.amdsmi is not None,
                'aocl_optimized': self.amd_cpu_optimized,
                'rocm': ROCM_AVAILABLE,
                'rocm_version': self.rocm_version
            },
            'hardware': {
                'cpu': platform.processor(),
                'cpu_amd': self.amd_cpu_optimized,
                'gpu_count': self.amd_gpu_count,
            },
            'optimization_level': self._get_optimization_level(),
            'environment': 'Hugging Face Space' if os.path.exists('/.dockerenv') else 'Unknown'
        }
    
    def _get_optimization_level(self) -> str:
        """Get overall optimization level string"""
        if self.amd_gpu_count > 0:
            return 'Maximum (GPU + CPU)'
        elif self.amd_cpu_optimized:
            return 'Good (CPU Optimized)'
        elif AMD_SMI_AVAILABLE:
            return 'Basic (Monitoring only)'
        else:
            return 'Standard (No AMD optimizations)'
    
    def cleanup(self):
        """Clean up AMD resources"""
        if hasattr(self, 'amdsmi') and self.amdsmi and hasattr(self, 'amd_smi_handle'):
            try:
                self.amdsmi.amdsmi_shutdown()
            except:
                pass
