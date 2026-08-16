import asyncio
import ssl
import uuid

import backoff
import websockets


class RetryableError(Exception):
    pass


class Core(object):
    def __init__(self, args, camera, logger):
        self.host = args.host
        self.token = args.token
        self.mac = args.mac
        self.args = args
        self.logger = logger
        self.cam = camera

        # Set up ssl context for requests
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.check_hostname = False
        self.ssl_context.verify_mode = ssl.CERT_NONE
        self.ssl_context.load_cert_chain(args.cert, args.cert)

    async def run(self) -> None:
        uri = "wss://{}:7442/camera/1.0/ws?token={}".format(self.host, self.token)
        headers = {"camera-mac": self.mac}
        if getattr(self.args, "sysid", None):
            # Real cameras also identify on the WSS handshake. camera-model
            # must be the HEX SYSID (e.g. 0xa598) — sending the model NAME
            # here gets a 400 from Protect's internal proxy.
            headers.update(
                {
                    "camera-model": f"0x{int(self.args.sysid, 0):04x}",
                    "camera-ip": self.args.ip,
                    "camera-firmware": self.args.fw_version,
                    "device-id": str(
                        uuid.uuid5(
                            uuid.NAMESPACE_DNS, f"unifi-cam-proxy-{self.mac}"
                        )
                    ),
                    "x-guid": str(uuid.uuid4()),
                }
            )
        has_connected = False

        @backoff.on_predicate(
            backoff.expo,
            lambda retryable: retryable,
            factor=2,
            jitter=None,
            max_value=10,
            logger=self.logger,
        )
        async def connect():
            nonlocal has_connected
            self.logger.info(
                "Creating ws connection to"
                f" wss://{self.host}:7442/camera/1.0/ws?token={self.token[:4]}…"
            )
            try:
                ws = await websockets.connect(
                    uri,
                    extra_headers=headers,
                    ssl=self.ssl_context,
                    subprotocols=["secure_transfer"],
                )
                has_connected = True
            except websockets.exceptions.InvalidStatusCode as e:
                if e.status_code == 403:
                    self.logger.error(
                        f"The token '{self.token}'"
                        " is invalid. Please generate a new one and try again."
                    )
                # Hitting rate-limiting
                elif e.status_code == 429:
                    return True
                raise
            except asyncio.exceptions.TimeoutError:
                self.logger.info(f"Connection to {self.host} timed out.")
                return True
            except ConnectionRefusedError:
                self.logger.info(f"Connection to {self.host} refused.")
                return True

            tasks = [
                asyncio.create_task(self.cam._run(ws)),
                asyncio.create_task(self.cam.run()),
            ]
            try:
                await asyncio.gather(*tasks)
            except RetryableError:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                return True
            finally:
                await self.cam.close()

        await connect()
