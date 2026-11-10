"""One-time: register the Exness MT5 demo account in MetaApi and print its account id.
Run from the 'Connect Exness' workflow. Needs METAAPI_TOKEN, EXNESS_LOGIN, EXNESS_PASSWORD, EXNESS_SERVER."""
import asyncio
import os


async def main():
    from metaapi_cloud_sdk import MetaApi

    api = MetaApi(os.environ["METAAPI_TOKEN"])
    login = os.environ["EXNESS_LOGIN"].strip()
    for acc in await api.metatrader_account_api.get_accounts_with_infinite_scroll_pagination():
        if str(acc.login) == login:
            print("Already registered. METAAPI_ACCOUNT_ID =", acc.id)
            return
    acc = await api.metatrader_account_api.create_account({
        "name": f"Exness {login}", "type": "cloud-g2", "login": login,
        "password": os.environ["EXNESS_PASSWORD"].strip(), "server": os.environ["EXNESS_SERVER"].strip(),
        "platform": "mt5", "magic": 20261110, "application": "MetaApi",
    })
    await acc.deploy()
    await acc.wait_connected()
    print("Connected. METAAPI_ACCOUNT_ID =", acc.id)


if __name__ == "__main__":
    asyncio.run(main())
