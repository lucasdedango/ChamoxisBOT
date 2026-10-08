import asyncio
import os
import logging
from pathlib import Path
from filelock import FileLock
import uvicorn
from manager.config import initialize
from chamoxis_common.logging import configure


async def serve():
    data = initialize()
    configure(data / "manager.log")
    from manager.api.server import create_app
    app = create_app()
    tasks = []
    bot = None
    if os.getenv("DISCORD_TOKEN"):
        from manager.discord.bot import bot
        from manager.discord import bot as discord_ui
        from manager.discord.conversation import Conversation
        discord_ui.conversation = Conversation(app.state.store)
        from manager.core.notifications import Notifications
        app.state.discord_status = "connecting"
        discord_task = asyncio.create_task(bot.start(os.environ["DISCORD_TOKEN"]), name="discord")
        def discord_done(task):
            if not task.cancelled() and task.exception():
                app.state.discord_status = "failed"
                if type(task.exception()).__name__ == "PrivilegedIntentsRequired":
                    logging.getLogger(__name__).error("Enable Message Content Intent in the Discord Developer Portal for configured conversational channels, or clear DISCORD_CONVERSATION_CHANNEL_IDS")
                else:
                    logging.getLogger(__name__).error("Discord connection failed; check token and network configuration")
        discord_task.add_done_callback(discord_done)
        tasks.append(discord_task)
        async def readiness():
            while True:
                if not discord_task.done():
                    app.state.discord_status = "ready" if bot.is_ready() else "connecting"
                await asyncio.sleep(2)
        tasks.append(asyncio.create_task(readiness(), name="discord-readiness"))
        tasks.append(asyncio.create_task(Notifications(app.state.store, bot).run(), name="notifications"))
    server = uvicorn.Server(uvicorn.Config(app, host=os.getenv("MANAGER_HOST", "127.0.0.1"),
                                         port=int(os.getenv("MANAGER_PORT", "8760")), access_log=False))
    try:
        await server.serve()
    finally:
        if bot:
            await bot.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def main():
    data = initialize()
    data.mkdir(parents=True, exist_ok=True)
    with FileLock(str(data / "manager.lock"), timeout=0):
        asyncio.run(serve())


if __name__ == "__main__":
    main()
