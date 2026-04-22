import asyncio
from copilot import CopilotClient
from copilot.generated.session_events import SessionEventType
from copilot.session import PermissionHandler

async def main():
    client = CopilotClient()
    await client.start()

    session = await client.create_session(on_permission_request=PermissionHandler.approve_all, model="gpt-5.4", streaming=True)
    response = await session.send_and_wait("Make a fix in a file: scripts/windows/python/workstation/DaVinciResolveStudio19/include/Setup.py to support both resolve 19 and 20", timeout=600)
    def handle(event):
        if event.type == SessionEventType.ASSISTANT_MESSAGE_DELTA:
            print(event.data.delta_content, end="", flush=True)
    session.on(handle)
    print(response.data.content)

    await client.stop()

asyncio.run(main())