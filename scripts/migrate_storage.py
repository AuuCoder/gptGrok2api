#!/usr/bin/env python3
"""
存储后端数据迁移脚本

用法：
  python scripts/migrate_storage.py --from json --to postgres
  python scripts/migrate_storage.py --from postgres --to git
  python scripts/migrate_storage.py --export accounts.json
  python scripts/migrate_storage.py --import accounts.json
"""

import argparse
import json
import os
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DATA_DIR = Path(__file__).resolve().parents[1] / "data"

from services.storage.factory import create_storage_backend


def export_to_json(output_file: str):
    """导出当前存储后端的数据到 JSON 文件"""
    print(f"[migrate] Exporting data to {output_file}")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    storage = create_storage_backend(DATA_DIR)
    accounts = storage.load_accounts()
    
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(accounts, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    
    print(f"[migrate] Exported {len(accounts)} accounts to {output_file}")


def import_from_json(input_file: str):
    """从 JSON 文件导入数据到当前存储后端"""
    print(f"[migrate] Importing data from {input_file}")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    input_path = Path(input_file)
    if not input_path.exists():
        print(f"[migrate] Error: File not found: {input_file}")
        sys.exit(1)
    
    try:
        accounts = json.loads(input_path.read_text(encoding="utf-8"))
        if not isinstance(accounts, list):
            print(f"[migrate] Error: Invalid JSON format, expected array")
            sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"[migrate] Error: Invalid JSON: {e}")
        sys.exit(1)
    
    storage = create_storage_backend(DATA_DIR)
    storage.save_accounts(accounts)
    
    print(f"[migrate] Imported {len(accounts)} accounts")


def migrate_data(from_backend: str, to_backend: str):
    """从一个存储后端迁移到另一个"""
    print(f"[migrate] Migrating from {from_backend} to {to_backend}")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # 保存原始环境变量
    original_backend = os.environ.get("STORAGE_BACKEND")
    
    try:
        # 从源后端读取数据
        os.environ["STORAGE_BACKEND"] = from_backend
        from_storage = create_storage_backend(DATA_DIR)
        accounts = from_storage.load_accounts()
        print(f"[migrate] Loaded {len(accounts)} accounts from {from_backend}")
        
        # 写入目标后端
        os.environ["STORAGE_BACKEND"] = to_backend
        to_storage = create_storage_backend(DATA_DIR)
        to_storage.save_accounts(accounts)
        print(f"[migrate] Saved {len(accounts)} accounts to {to_backend}")
        
        print(f"[migrate] Migration completed successfully!")
        
    finally:
        # 恢复原始环境变量
        if original_backend:
            os.environ["STORAGE_BACKEND"] = original_backend
        elif "STORAGE_BACKEND" in os.environ:
            del os.environ["STORAGE_BACKEND"]


def main():
    parser = argparse.ArgumentParser(
        description="ChatGPT2API 存储后端数据迁移工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 从 JSON 迁移到 PostgreSQL
  python scripts/migrate_storage.py --from json --to postgres
  
  # 从 PostgreSQL 迁移到 Git
  python scripts/migrate_storage.py --from postgres --to git
  
  # 导出当前数据到 JSON 文件
  python scripts/migrate_storage.py --export backup.json
  
  # 从 JSON 文件导入数据
  python scripts/migrate_storage.py --import backup.json

环境变量:
  STORAGE_BACKEND  - 存储后端类型 (json, sqlite, postgres, git)
  DATABASE_URL     - 数据库连接字符串
  GIT_REPO_URL     - Git 仓库地址
  GIT_TOKEN        - Git 访问令牌
        """
    )
    
    parser.add_argument(
        "--from",
        dest="from_backend",
        choices=["json", "sqlite", "postgres", "git"],
        help="源存储后端",
    )
    parser.add_argument(
        "--to",
        dest="to_backend",
        choices=["json", "sqlite", "postgres", "git"],
        help="目标存储后端",
    )
    parser.add_argument(
        "--export",
        dest="export_file",
        metavar="FILE",
        help="导出数据到 JSON 文件",
    )
    parser.add_argument(
        "--import",
        dest="import_file",
        metavar="FILE",
        help="从 JSON 文件导入数据",
    )
    
    args = parser.parse_args()
    
    # 检查参数
    if args.from_backend and args.to_backend:
        migrate_data(args.from_backend, args.to_backend)
    elif args.export_file:
        export_to_json(args.export_file)
    elif args.import_file:
        import_from_json(args.import_file)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
