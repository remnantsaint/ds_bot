"""独立命令入口：python -m src.memory_tool status / search / run。"""
import argparse
import asyncio
import json


async def main():
    parser = argparse.ArgumentParser(description='本地群聊记忆工具')
    parser.add_argument('command', choices=['status', 'search', 'run','setup-tokenizer'])
    parser.add_argument('--bot')
    parser.add_argument('--group')
    parser.add_argument('--query')
    args = parser.parse_args()
    if args.command=='setup-tokenizer':
        from src.token_budget import install_tokenizer,mode
        install_tokenizer()
        print(mode())
        return
    import nonebot
    nonebot.init(driver='~fastapi', log_level='ERROR')
    from src.memory import get_memory
    service = get_memory()
    if not service:
        parser.error('记忆未启用，请先在后台启用')
    if args.command == 'status':
        result = service.store.status()
    elif args.command == 'search':
        if not (args.bot and args.group and args.query):
            parser.error('检索需要 --bot --group --query')
        result = service.store.search(args.bot, args.group, args.query)
    else:
        # 手工运行旧日期整理及当天达到阈值的增量整理。
        try:
            result = await service.run(force=True)
        finally:
            from src.deepseek_client import deepseek_client
            await deepseek_client.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    service.store.db.close()


if __name__ == '__main__':
    asyncio.run(main())
