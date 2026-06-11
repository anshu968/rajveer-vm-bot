# main.py
import discord
from discord.ext import commands, tasks
import json
import os
import subprocess
import uuid
import asyncio
import yaml
from datetime import datetime, timedelta
import sqlite3
import psutil
import re

# Load configuration
with open('config.yml', 'r') as f:
    config = yaml.safe_load(f)

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix=config['prefix'], intents=intents)

# Database setup
conn = sqlite3.connect('vps_bot.db')
c = conn.cursor()

# Create tables
c.execute('''CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    coins INTEGER DEFAULT 0,
    total_invites INTEGER DEFAULT 0
)''')

c.execute('''CREATE TABLE IF NOT EXISTS vps_instances (
    vm_id TEXT PRIMARY KEY,
    user_id TEXT,
    ram INTEGER,
    cpu INTEGER,
    disk INTEGER,
    os TEXT,
    status TEXT,
    created_at TIMESTAMP,
    expires_at TIMESTAMP,
    ssh_link TEXT
)''')

c.execute('''CREATE TABLE IF NOT EXISTS invites (
    inviter_id TEXT,
    invited_id TEXT,
    timestamp TIMESTAMP
)''')

conn.commit()

# VM Manager Class
class VMManager:
    def __init__(self):
        self.base_path = config['vm_storage_path']
        os.makedirs(self.base_path, exist_ok=True)
        
    def create_vm(self, vm_id, ram_mb, cpu_cores, disk_gb, user):
        """Create a new VM using QEMU/KVM"""
        vm_path = f"{self.base_path}/{vm_id}"
        os.makedirs(vm_path, exist_ok=True)
        
        # Create disk image
        disk_path = f"{vm_path}/disk.qcow2"
        subprocess.run([
            'qemu-img', 'create', '-f', 'qcow2', disk_path, f'{disk_gb}G'
        ], check=True)
        
        # Generate cloud-init config
        self._generate_cloud_init(vm_path, user)
        
        # Create start script
        self._create_start_script(vm_id, vm_path, ram_mb, cpu_cores, disk_path)
        
        return True
    
    def _generate_cloud_init(self, vm_path, user):
        """Generate cloud-init configuration"""
        user_data = f"""#cloud-config
users:
  - name: {user}
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    ssh_authorized_keys:
      - {config['admin_ssh_key']}
package_update: true
packages:
  - tmate
  - htop
  - git
  - curl
  - wget
"""
        with open(f"{vm_path}/user-data", 'w') as f:
            f.write(user_data)
        
        # Generate meta-data
        meta_data = f"instance-id: {vm_path}\nlocal-hostname: vm-{vm_id}"
        with open(f"{vm_path}/meta-data", 'w') as f:
            f.write(meta_data)
        
        # Create cloud-init ISO
        subprocess.run([
            'genisoimage', '-output', f"{vm_path}/cloud-init.iso",
            '-volid', 'cidata', '-joliet', '-rock',
            f"{vm_path}/user-data", f"{vm_path}/meta-data"
        ], check=True)
    
    def _create_start_script(self, vm_id, vm_path, ram_mb, cpu_cores, disk_path):
        """Create VM start script"""
        script_content = f"""#!/bin/bash
sudo qemu-system-x86_64 \\
  -name {vm_id} \\
  -machine type=q35,accel=kvm \\
  -cpu host \\
  -smp {cpu_cores} \\
  -m {ram_mb} \\
  -drive file={disk_path},if=virtio,format=qcow2 \\
  -drive file={vm_path}/cloud-init.iso,if=virtio,media=cdrom \\
  -netdev user,id=net0,hostfwd=tcp::{config['ssh_port_range'][0] + int(vm_id)}-:22 \\
  -device virtio-net-pci,netdev=net0 \\
  -vnc :{int(vm_id)} \\
  -daemonize
"""
        with open(f"{vm_path}/start.sh", 'w') as f:
            f.write(script_content)
        os.chmod(f"{vm_path}/start.sh", 0o755)
    
    def start_vm(self, vm_id):
        """Start a VM"""
        vm_path = f"{self.base_path}/{vm_id}"
        if os.path.exists(f"{vm_path}/start.sh"):
            result = subprocess.run([f"{vm_path}/start.sh"], capture_output=True, text=True)
            return result.returncode == 0
        return False
    
    def stop_vm(self, vm_id):
        """Stop a VM"""
        result = subprocess.run(['pkill', '-f', f'qemu-system-x86_64.*name {vm_id}'], 
                              capture_output=True)
        return result.returncode == 0
    
    def restart_vm(self, vm_id):
        """Restart a VM"""
        self.stop_vm(vm_id)
        time.sleep(2)
        return self.start_vm(vm_id)
    
    def delete_vm(self, vm_id):
        """Delete a VM"""
        self.stop_vm(vm_id)
        vm_path = f"{self.base_path}/{vm_id}"
        subprocess.run(['rm', '-rf', vm_path])
        return True
    
    def get_tmate_ssh(self, vm_id):
        """Get tmate SSH link from VM"""
        # This would need to exec into VM and get tmate output
        # Simplified version - you'd need to implement SSH connection
        return f"ssh://tmate-session-{vm_id}@tmate.io"

vm_manager = VMManager()

# Coin Manager
class CoinManager:
    @staticmethod
    def add_coins(user_id, amount):
        c.execute("INSERT OR IGNORE INTO users (user_id, coins) VALUES (?, ?)", (user_id, 0))
        c.execute("UPDATE users SET coins = coins + ? WHERE user_id = ?", (amount, user_id))
        conn.commit()
    
    @staticmethod
    def remove_coins(user_id, amount):
        c.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,))
        result = c.fetchone()
        if result and result[0] >= amount:
            c.execute("UPDATE users SET coins = coins - ? WHERE user_id = ?", (amount, user_id))
            conn.commit()
            return True
        return False
    
    @staticmethod
    def get_balance(user_id):
        c.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,))
        result = c.fetchone()
        return result[0] if result else 0

coin_manager = CoinManager()

# Admin Check
def is_admin():
    async def predicate(ctx):
        return ctx.author.id in config['admin_ids']
    return commands.check(predicate)

# Events
@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')
    check_expired_vms.start()

@bot.event
async def on_member_join(member):
    # Track invites
    invites = await member.guild.invites()
    for invite in invites:
        if invite.inviter.id != member.id:
            coin_manager.add_coins(str(invite.inviter.id), config['invite_reward'])
            c.execute("INSERT INTO invites (inviter_id, invited_id, timestamp) VALUES (?, ?, ?)",
                     (str(invite.inviter.id), str(member.id), datetime.now()))
            conn.commit()
            break

# Admin Commands
@bot.command(name='deploy')
@is_admin()
async def deploy_vm(ctx, ram: int, cpu: int, disk: int, user: str):
    """Admin only: Deploy VM to user"""
    vm_id = str(uuid.uuid4())[:8]
    
    embed = discord.Embed(title="🚀 Deploying VM", color=discord.Color.green())
    embed.add_field(name="VM ID", value=vm_id, inline=True)
    embed.add_field(name="RAM", value=f"{ram}MB", inline=True)
    embed.add_field(name="CPU", value=f"{cpu} cores", inline=True)
    embed.add_field(name="Disk", value=f"{disk}GB", inline=True)
    embed.add_field(name="User", value=user, inline=True)
    
    await ctx.send(embed=embed)
    
    # Create VM
    try:
        vm_manager.create_vm(vm_id, ram, cpu, disk, user)
        
        # Store in database
        c.execute("INSERT INTO vps_instances VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (vm_id, str(ctx.author.id), ram, cpu, disk, config['default_os'],
                  'stopped', datetime.now(), 
                  datetime.now() + timedelta(days=config['vm_lifetime_days']),
                  None))
        conn.commit()
        
        embed = discord.Embed(title="✅ VM Deployed Successfully!", color=discord.Color.green())
        embed.add_field(name="VM ID", value=vm_id, inline=False)
        embed.add_field(name="User", value=f"<@{ctx.author.id}> can manage this VM", inline=False)
        await ctx.send(embed=embed)
        
    except Exception as e:
        await ctx.send(f"❌ Failed to deploy VM: {str(e)}")

@bot.command(name='givecoin')
@is_admin()
async def give_coin(ctx, amount: int, user: discord.User):
    """Admin only: Give coins to user"""
    coin_manager.add_coins(str(user.id), amount)
    embed = discord.Embed(title="💰 Coins Given!", color=discord.Color.gold())
    embed.add_field(name="User", value=user.mention, inline=True)
    embed.add_field(name="Amount", value=f"{amount} coins", inline=True)
    embed.add_field(name="New Balance", value=f"{coin_manager.get_balance(str(user.id))} coins", inline=True)
    await ctx.send(embed=embed)

@bot.command(name='delete_vm')
@is_admin()
async def delete_vm_admin(ctx, vm_id: str):
    """Admin only: Delete any VM"""
    c.execute("SELECT * FROM vps_instances WHERE vm_id = ?", (vm_id,))
    vm = c.fetchone()
    
    if vm:
        vm_manager.delete_vm(vm_id)
        c.execute("DELETE FROM vps_instances WHERE vm_id = ?", (vm_id,))
        conn.commit()
        await ctx.send(f"✅ VM {vm_id} has been deleted!")
    else:
        await ctx.send("❌ VM not found!")

@bot.command(name='shop_add')
@is_admin()
async def add_shop_item(ctx, item_name: str, ram: int, cpu: int, disk: int, price: int):
    """Admin only: Add item to shop"""
    if 'shop_items' not in config:
        config['shop_items'] = []
    
    config['shop_items'].append({
        'name': item_name,
        'ram': ram,
        'cpu': cpu,
        'disk': disk,
        'price': price
    })
    
    with open('config.yml', 'w') as f:
        yaml.dump(config, f)
    
    await ctx.send(f"✅ Added {item_name} to shop for {price} coins!")

# User Commands
@bot.command(name='create_vps')
async def create_vps(ctx, ram: int = None, cpu: int = None, disk: int = None):
    """Create a new VPS (interactive mode)"""
    user_id = str(ctx.author.id)
    
    # Check user's current VPS count
    c.execute("SELECT COUNT(*) FROM vps_instances WHERE user_id = ?", (user_id,))
    count = c.fetchone()[0]
    
    if count >= config['max_vps_per_user']:
        await ctx.send(f"❌ You can only have up to {config['max_vps_per_user']} VPS instances!")
        return
    
    # Get specs
    if ram is None or cpu is None or disk is None:
        embed = discord.Embed(title="📝 VPS Creation Wizard", description="Please provide the specs:", color=discord.Color.blue())
        embed.add_field(name="Max Specs", value=f"RAM: {config['max_ram']}MB\nCPU: {config['max_cpu']} cores\nDisk: Unlimited", inline=False)
        await ctx.send(embed=embed)
        
        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel
        
        try:
            await ctx.send("Enter RAM (MB):")
            ram_msg = await bot.wait_for('message', timeout=60, check=check)
            ram = int(ram_msg.content)
            
            await ctx.send("Enter CPU cores:")
            cpu_msg = await bot.wait_for('message', timeout=60, check=check)
            cpu = int(cpu_msg.content)
            
            await ctx.send("Enter Disk (GB):")
            disk_msg = await bot.wait_for('message', timeout=60, check=check)
            disk = int(disk_msg.content)
        except:
            await ctx.send("❌ Timeout or invalid input!")
            return
    
    # Validate specs
    if ram > config['max_ram'] or cpu > config['max_cpu']:
        await ctx.send(f"❌ Specs exceed maximum!\nMax RAM: {config['max_ram']}MB\nMax CPU: {config['max_cpu']} cores")
        return
    
    # Calculate price
    price = (ram // 512) * 5 + (cpu * 10) + (disk // 10) * 2
    
    # Confirm creation
    embed = discord.Embed(title="🖥️ VPS Creation Confirmation", color=discord.Color.orange())
    embed.add_field(name="RAM", value=f"{ram}MB", inline=True)
    embed.add_field(name="CPU", value=f"{cpu} cores", inline=True)
    embed.add_field(name="Disk", value=f"{disk}GB", inline=True)
    embed.add_field(name="Price", value=f"{price} coins", inline=True)
    embed.add_field(name="Your Balance", value=f"{coin_manager.get_balance(user_id)} coins", inline=True)
    embed.set_footer(text="Reply with 'confirm' to proceed or 'cancel' to abort")
    
    await ctx.send(embed=embed)
    
    def check(m):
        return m.author == ctx.author and m.channel == ctx.channel and m.content.lower() in ['confirm', 'cancel']
    
    try:
        response = await bot.wait_for('message', timeout=30, check=check)
        
        if response.content.lower() == 'confirm':
            if coin_manager.remove_coins(user_id, price):
                vm_id = str(uuid.uuid4())[:8]
                
                # Create VM
                vm_manager.create_vm(vm_id, ram, cpu, disk, ctx.author.name)
                
                # Store in database
                c.execute("INSERT INTO vps_instances VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (vm_id, user_id, ram, cpu, disk, config['default_os'],
                          'stopped', datetime.now(),
                          datetime.now() + timedelta(days=config['vm_lifetime_days']),
                          None))
                conn.commit()
                
                embed = discord.Embed(title="✅ VPS Created Successfully!", color=discord.Color.green())
                embed.add_field(name="VM ID", value=vm_id, inline=False)
                embed.add_field(name="Use", value="`/manage` to control your VPS", inline=False)
                await ctx.send(embed=embed)
            else:
                await ctx.send("❌ Insufficient coins!")
        else:
            await ctx.send("❌ Creation cancelled!")
    except:
        await ctx.send("❌ Timeout!")

@bot.command(name='manage')
async def manage_vps(ctx):
    """Manage your VPS instances"""
    user_id = str(ctx.author.id)
    
    c.execute("SELECT vm_id, ram, cpu, disk, status FROM vps_instances WHERE user_id = ?", (user_id,))
    vms = c.fetchall()
    
    if not vms:
        await ctx.send("❌ You don't have any VPS instances!")
        return
    
    embed = discord.Embed(title="🖥️ Your VPS Instances", color=discord.Color.blue())
    
    for vm in vms:
        embed.add_field(
            name=f"VM ID: {vm[0]}",
            value=f"RAM: {vm[1]}MB | CPU: {vm[2]} cores | Disk: {vm[3]}GB\nStatus: {vm[4]}",
            inline=False
        )
    
    embed.set_footer(text="Use /start <vm_id>, /stop <vm_id>, /restart <vm_id>, /ssh <vm_id>")
    
    await ctx.send(embed=embed)

@bot.command(name='start')
async def start_vm(ctx, vm_id: str):
    """Start a VPS instance"""
    user_id = str(ctx.author.id)
    
    c.execute("SELECT * FROM vps_instances WHERE vm_id = ? AND user_id = ?", (vm_id, user_id))
    vm = c.fetchone()
    
    if vm:
        if vm_manager.start_vm(vm_id):
            c.execute("UPDATE vps_instances SET status = 'running' WHERE vm_id = ?", (vm_id,))
            conn.commit()
            await ctx.send(f"✅ VM {vm_id} started successfully!")
        else:
            await ctx.send(f"❌ Failed to start VM {vm_id}")
    else:
        await ctx.send("❌ VM not found or you don't own it!")

@bot.command(name='stop')
async def stop_vm(ctx, vm_id: str):
    """Stop a VPS instance"""
    user_id = str(ctx.author.id)
    
    c.execute("SELECT * FROM vps_instances WHERE vm_id = ? AND user_id = ?", (vm_id, user_id))
    vm = c.fetchone()
    
    if vm:
        if vm_manager.stop_vm(vm_id):
            c.execute("UPDATE vps_instances SET status = 'stopped' WHERE vm_id = ?", (vm_id,))
            conn.commit()
            await ctx.send(f"✅ VM {vm_id} stopped successfully!")
        else:
            await ctx.send(f"❌ Failed to stop VM {vm_id}")
    else:
        await ctx.send("❌ VM not found or you don't own it!")

@bot.command(name='restart')
async def restart_vm(ctx, vm_id: str):
    """Restart a VPS instance"""
    user_id = str(ctx.author.id)
    
    c.execute("SELECT * FROM vps_instances WHERE vm_id = ? AND user_id = ?", (vm_id, user_id))
    vm = c.fetchone()
    
    if vm:
        if vm_manager.restart_vm(vm_id):
            c.execute("UPDATE vps_instances SET status = 'running' WHERE vm_id = ?", (vm_id,))
            conn.commit()
            await ctx.send(f"✅ VM {vm_id} restarted successfully!")
        else:
            await ctx.send(f"❌ Failed to restart VM {vm_id}")
    else:
        await ctx.send("❌ VM not found or you don't own it!")

@bot.command(name='ssh')
async def get_ssh(ctx, vm_id: str):
    """Get SSH access to your VPS"""
    user_id = str(ctx.author.id)
    
    c.execute("SELECT * FROM vps_instances WHERE vm_id = ? AND user_id = ?", (vm_id, user_id))
    vm = c.fetchone()
    
    if vm:
        ssh_link = vm_manager.get_tmate_ssh(vm_id)
        embed = discord.Embed(title="🔐 SSH Access", color=discord.Color.green())
        embed.add_field(name="SSH Link", value=ssh_link, inline=False)
        embed.add_field(name="Username", value=ctx.author.name, inline=True)
        embed.add_field(name="Port", value=config['ssh_port_range'][0] + int(vm_id), inline=True)
        embed.set_footer(text="Use tmate to share your SSH session")
        await ctx.send(embed=embed)
    else:
        await ctx.send("❌ VM not found or you don't own it!")

@bot.command(name='reinstall')
async def reinstall_os(ctx, vm_id: str):
    """Reinstall OS on your VPS (deletes all data)"""
    user_id = str(ctx.author.id)
    
    c.execute("SELECT * FROM vps_instances WHERE vm_id = ? AND user_id = ?", (vm_id, user_id))
    vm = c.fetchone()
    
    if vm:
        await ctx.send("⚠️ **WARNING**: This will delete ALL data on your VPS! Type 'CONFIRM' to proceed.")
        
        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel and m.content == 'CONFIRM'
        
        try:
            await bot.wait_for('message', timeout=30, check=check)
            
            # Delete and recreate VM
            vm_manager.delete_vm(vm_id)
            vm_manager.create_vm(vm_id, vm[2], vm[3], vm[4], ctx.author.name)
            
            await ctx.send(f"✅ VM {vm_id} has been reinstalled with {config['default_os']}!")
        except:
            await ctx.send("❌ Reinstall cancelled!")
    else:
        await ctx.send("❌ VM not found or you don't own it!")

@bot.command(name='balance')
async def check_balance(ctx):
    """Check your coin balance"""
    user_id = str(ctx.author.id)
    balance = coin_manager.get_balance(user_id)
    
    embed = discord.Emblem(title="💰 Coin Balance", color=discord.Color.gold())
    embed.add_field(name="User", value=ctx.author.mention, inline=True)
    embed.add_field(name="Balance", value=f"{balance} coins", inline=True)
    embed.add_field(name="Invites", value=f"{config['invite_reward']} coins per invite", inline=False)
    
    await ctx.send(embed=embed)

@bot.command(name='gift')
async def gift_coins(ctx, amount: int, user: discord.User):
    """Gift coins to another user"""
    sender_id = str(ctx.author.id)
    receiver_id = str(user.id)
    
    if coin_manager.remove_coins(sender_id, amount):
        coin_manager.add_coins(receiver_id, amount)
        await ctx.send(f"✅ You gifted {amount} coins to {user.mention}!")
    else:
        await ctx.send("❌ Insufficient coins!")

@bot.command(name='shop')
async def view_shop(ctx):
    """View available VPS packages"""
    if 'shop_items' not in config or not config['shop_items']:
        await ctx.send("❌ No items in shop yet! Admin can add items using `/shop_add`")
        return
    
    embed = discord.Embed(title="🛒 VPS Shop", color=discord.Color.blue())
    
    for item in config['shop_items']:
        embed.add_field(
            name=f"{item['name']} - {item['price']} coins",
            value=f"RAM: {item['ram']}MB | CPU: {item['cpu']} cores | Disk: {item['disk']}GB",
            inline=False
        )
    
    embed.set_footer(text="Use /buy <item_name> to purchase")
    await ctx.send(embed=embed)

@bot.command(name='buy')
async def buy_vps(ctx, *, item_name: str):
    """Purchase a VPS from shop"""
    user_id = str(ctx.author.id)
    
    # Find item
    item = None
    for shop_item in config.get('shop_items', []):
        if shop_item['name'].lower() == item_name.lower():
            item = shop_item
            break
    
    if not item:
        await ctx.send("❌ Item not found!")
        return
    
    # Check balance
    if coin_manager.get_balance(user_id) >= item['price']:
        # Check VPS limit
        c.execute("SELECT COUNT(*) FROM vps_instances WHERE user_id = ?", (user_id,))
        count = c.fetchone()[0]
        
        if count >= config['max_vps_per_user']:
            await ctx.send(f"❌ You've reached the maximum of {config['max_vps_per_user']} VPS instances!")
            return
        
        # Create VPS
        vm_id = str(uuid.uuid4())[:8]
        vm_manager.create_vm(vm_id, item['ram'], item['cpu'], item['disk'], ctx.author.name)
        
        c.execute("INSERT INTO vps_instances VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (vm_id, user_id, item['ram'], item['cpu'], item['disk'], config['default_os'],
                  'stopped', datetime.now(),
                  datetime.now() + timedelta(days=config['vm_lifetime_days']),
                  None))
        conn.commit()
        
        coin_manager.remove_coins(user_id, item['price'])
        
        await ctx.send(f"✅ Purchased {item['name']} VPS!\nVM ID: {vm_id}\nUse `/manage` to control it")
    else:
        await ctx.send("❌ Insufficient coins!")

@bot.command(name='info')
async def vps_info(ctx, vm_id: str):
    """Get detailed information about your VPS"""
    user_id = str(ctx.author.id)
    
    c.execute("SELECT * FROM vps_instances WHERE vm_id = ? AND user_id = ?", (vm_id, user_id))
    vm = c.fetchone()
    
    if vm:
        embed = discord.Embed(title=f"🖥️ VPS Information - {vm_id}", color=discord.Color.blue())
        embed.add_field(name="RAM", value=f"{vm[2]}MB", inline=True)
        embed.add_field(name="CPU", value=f"{vm[3]} cores", inline=True)
        embed.add_field(name="Disk", value=f"{vm[4]}GB", inline=True)
        embed.add_field(name="OS", value=vm[5], inline=True)
        embed.add_field(name="Status", value=vm[6], inline=True)
        embed.add_field(name="Created", value=vm[7][:19], inline=True)
        embed.add_field(name="Expires", value=vm[8][:19], inline=True)
        
        await ctx.send(embed=embed)
    else:
        await ctx.send("❌ VM not found or you don't own it!")

@bot.command(name='stats')
@is_admin()
async def bot_stats(ctx):
    """View bot statistics"""
    c.execute("SELECT COUNT(*) FROM vps_instances")
    total_vms = c.fetchone()[0]
    
    c.execute("SELECT SUM(coins) FROM users")
    total_coins = c.fetchone()[0] or 0
    
    c.execute("SELECT COUNT(DISTINCT user_id) FROM vps_instances")
    unique_users = c.fetchone()[0]
    
    embed = discord.Embed(title="📊 Bot Statistics", color=discord.Color.green())
    embed.add_field(name="Total VMs", value=total_vms, inline=True)
    embed.add_field(name="Total Users", value=unique_users, inline=True)
    embed.add_field(name="Total Coins in Circulation", value=total_coins, inline=True)
    embed.add_field(name="Servers", value=len(bot.guilds), inline=True)
    
    await ctx.send(embed=embed)

# Background tasks
@tasks.loop(hours=24)
async def check_expired_vms():
    """Check for expired VMS and delete them"""
    c.execute("SELECT vm_id FROM vps_instances WHERE expires_at < ?", (datetime.now(),))
    expired_vms = c.fetchall()
    
    for vm in expired_vms:
        vm_manager.delete_vm(vm[0])
        c.execute("DELETE FROM vps_instances WHERE vm_id = ?", (vm[0],))
    
    conn.commit()

# Error handling
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You don't have permission to use this command!")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Missing required arguments!")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ Invalid argument!")
    else:
        await ctx.send(f"❌ An error occurred: {str(error)}")
        print(error)

# Run bot
if __name__ == "__main__":
    bot.run(config['bot_token'])