# vm_helper.py
import subprocess
import json
import os

class AdvancedVMManager:
    """Advanced VM management with monitoring"""
    
    @staticmethod
    def get_vm_stats(vm_id):
        """Get VM resource usage"""
        try:
            # Get VM process info
            result = subprocess.run(
                ['ps', 'aux'], 
                capture_output=True, 
                text=True
            )
            
            for line in result.stdout.split('\n'):
                if f'qemu-system-x86_64' in line and vm_id in line:
                    parts = line.split()
                    return {
                        'cpu': float(parts[2]),
                        'memory': float(parts[3]),
                        'pid': int(parts[1])
                    }
            return None
        except:
            return None
    
    @staticmethod
    def backup_vm(vm_id):
        """Create VM backup"""
        vm_path = f"/var/lib/libvirt/rajveer-vms/{vm_id}"
        backup_path = f"{vm_path}_backup_{int(time.time())}"
        
        subprocess.run(['cp', '-r', vm_path, backup_path])
        return backup_path
    
    @staticmethod
    def migrate_vm(vm_id, target_host):
        """Migrate VM to another host"""
        # This would require additional setup
        pass
    
    @staticmethod
    def get_vm_logs(vm_id):
        """Get VM logs"""
        log_path = f"/var/log/libvirt/qemu/{vm_id}.log"
        if os.path.exists(log_path):
            with open(log_path, 'r') as f:
                return f.read()[-5000:]  # Last 5000 lines
        return "No logs found"

# Monitoring system
class MonitorSystem:
    @staticmethod
    def check_host_resources():
        """Monitor host resources"""
        import psutil
        
        cpu_percent = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        
        return {
            'cpu': cpu_percent,
            'memory_used': memory.percent,
            'disk_used': disk.percent,
            'total_vms': len([d for d in os.listdir('/var/lib/libvirt/rajveer-vms') 
                             if os.path.isdir(os.path.join('/var/lib/libvirt/rajveer-vms', d))])
        }