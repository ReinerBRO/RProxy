#!/usr/bin/env python3
"""账号池管理脚本"""
import json
import sys
from pathlib import Path

ACCOUNTS_FILE = Path('/home/ubuntu/valid_accounts.json')
KEYS_FILE = Path('keys.json')

def list_pools():
    """列出所有池的状态"""
    with open(ACCOUNTS_FILE) as f:
        accounts = json.load(f)
    
    pools = {}
    for acc in accounts:
        pool = acc.get('pool', 'free')
        pools[pool] = pools.get(pool, 0) + 1
    
    print('账号池状态:')
    for pool, count in sorted(pools.items()):
        print(f'  {pool}: {count} 个账号')

def add_account_to_pool(account_file, pool_name):
    """将指定账号添加到池"""
    with open(ACCOUNTS_FILE) as f:
        accounts = json.load(f)
    
    found = False
    for acc in accounts:
        if acc['file'] == account_file:
            acc['pool'] = pool_name
            found = True
            break
    
    if not found:
        print(f'错误: 找不到账号文件 {account_file}')
        return False
    
    with open(ACCOUNTS_FILE, 'w') as f:
        json.dump(accounts, f, indent=2)
    
    print(f'已将账号 {account_file} 移动到 {pool_name} 池')
    return True

def create_key(key_name, pool_name, quota_usd=0.0):
    """创建新的API key"""
    import secrets
    
    # 生成新的key
    new_key = f'sk-{secrets.token_urlsafe(48)}'
    
    with open(KEYS_FILE) as f:
        keys = json.load(f)
    
    keys[new_key] = {
        'name': key_name,
        'quota_usd': quota_usd,
        'enabled': True,
        'pool': pool_name
    }
    
    with open(KEYS_FILE, 'w') as f:
        json.dump(keys, f, indent=2)
    
    print(f'已创建新key:')
    print(f'  Key: {new_key}')
    print(f'  Name: {key_name}')
    print(f'  Pool: {pool_name}')
    print(f'  Quota: ')
    return new_key

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('用法:')
        print('  python3 manage_pools.py list                              # 列出池状态')
        print('  python3 manage_pools.py move <account_file> <pool>        # 移动账号到指定池')
        print('  python3 manage_pools.py create-key <name> <pool> [quota]  # 创建新key')
        sys.exit(1)
    
    cmd = sys.argv[1]
    
    if cmd == 'list':
        list_pools()
    elif cmd == 'move' and len(sys.argv) >= 4:
        add_account_to_pool(sys.argv[2], sys.argv[3])
    elif cmd == 'create-key' and len(sys.argv) >= 4:
        quota = float(sys.argv[4]) if len(sys.argv) > 4 else 0.0
        create_key(sys.argv[2], sys.argv[3], quota)
    else:
        print('无效的命令')
        sys.exit(1)
