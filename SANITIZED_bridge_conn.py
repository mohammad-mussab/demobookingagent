#!/usr/bin/env python3
"""
DEMO VERSION - Pipecat-Talkdesk Bridge Server
==============================================

This is a SANITIZED version for demonstration purposes.
All credentials and sensitive data have been removed.

PURPOSE:
--------
This bridge service connects Talkdesk telephony platform with the Pipecat AI agent.

FUNCTIONALITY:
--------------
1. Audio Format Conversion: μ-law 8kHz (Talkdesk) ↔ PCM 16kHz (Pipecat)
2. WebSocket Forwarding: Bidirectional audio streaming
3. Session Management: Redis caching for call metadata
4. Business Hours Routing: Dynamic flow initialization based on open/closed status
5. Call Logging: MySQL database for call records
6. Escalation Handling: Transfer calls to human operators

ARCHITECTURE:
-------------
┌──────────┐         ┌──────────┐         ┌──────────┐
│ Talkdesk │ μ-law  │  Bridge  │  PCM   │  Pipecat │
│ (8kHz)   │ ──────>│ (This)   │ ──────>│  (16kHz) │
└──────────┘         └──────────┘         └──────────┘

DEPLOYMENT:
-----------
- Hosted on Azure VM
- Runs on port 8080
- Connects to Pipecat agent via WebSocket
- Uses Redis for session state
- Uses MySQL for call logging
"""

import redis
import asyncio
import websockets
import json
import base64
import audioop
import logging
import signal
import sys
import uuid
import os
import time
from typing import Optional, Dict, Any, Set
from dataclasses import dataclass
from enum import Enum

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
import aiohttp
import aiomysql

# ============================================================================
# CONFIGURATION
# ============================================================================

# Active sessions tracking
ACTIVE_SESSIONS: Dict[str, "BridgeSession"] = {}

# MySQL Configuration (DEMO - use environment variables in production)
DB_CONFIG = {
    "host": os.getenv("MYSQL_HOST", "your-mysql-host.database.azure.com"),
    "port": int(os.getenv("MYSQL_PORT", 3306)),
    "user": os.getenv("MYSQL_USER", "your_username"),
    "password": os.getenv("MYSQL_PASSWORD", "your_password"),
    "db": os.getenv("MYSQL_DATABASE", "your_database"),
    "charset": "utf8",
    "autocommit": True,
    "connect_timeout": 2000,
    "ssl": {}
}

# Redis Configuration (DEMO - use environment variables in production)
REDIS_HOST = os.getenv("REDIS_HOST", "your-redis.cache.windows.net")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6380))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "your_redis_password")
REDIS_TTL = 24 * 3600  # 24 hours

redis_client = redis.StrictRedis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    password=REDIS_PASSWORD,
    ssl=True
)

# Pipecat Configuration
PIPECAT_SERVER_URL = os.getenv("PIPECAT_SERVER_URL", "ws://localhost:8000/ws")
PIPECAT_ASSISTANT_ID = os.getenv("PIPECAT_ASSISTANT_ID", "12689")
PORT = int(os.getenv("PORT", 8080))

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bridge.log')
    ]
)
logger = logging.getLogger('PipecatBridge')

# ============================================================================
# ENUMS
# ============================================================================

class ConnectionState(Enum):
    """Pipecat WebSocket connection states"""
    INIT = "init"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    CLOSING = "closing"
    CLOSED = "closed"
    ERROR = "error"

class BridgeState(Enum):
    """Bridge session states"""
    WAITING_START = "waiting_start"
    ACTIVE = "active"
    ESCALATING = "escalating"
    PIPECAT_CLOSED = "pipecat_closed"
    CLOSING = "closing"
    CLOSED = "closed"

# ============================================================================
# CONFIGURATION DATACLASS
# ============================================================================

@dataclass
class BridgeConfig:
    """Bridge configuration parameters"""
    host: str = "0.0.0.0"
    port: int = PORT
    pipecat_server_url: str = PIPECAT_SERVER_URL
    pipecat_assistant_id: str = PIPECAT_ASSISTANT_ID
    talkdesk_sample_rate: int = 8000
    pipecat_sample_rate: int = 16000
    channels: int = 1
    chunk_size: int = 160

cfg = BridgeConfig()

# ============================================================================
# DATABASE FUNCTIONS
# ============================================================================

async def save_call_to_mysql(call_id: str, assistant_id: str, interaction_id: str, phone_number: str = "") -> bool:
    """
    Save call record to MySQL database.

    Args:
        call_id: Pipecat session ID
        assistant_id: Agent identifier
        interaction_id: Talkdesk interaction ID
        phone_number: Caller phone number

    Returns:
        bool: Success status
    """
    connection = None
    try:
        connection = await aiomysql.connect(
            host=DB_CONFIG["host"],
            port=DB_CONFIG["port"],
            user=DB_CONFIG["user"],
            password=DB_CONFIG["password"],
            db=DB_CONFIG["db"],
            charset=DB_CONFIG["charset"],
            autocommit=DB_CONFIG["autocommit"],
            connect_timeout=DB_CONFIG["connect_timeout"],
            ssl=DB_CONFIG["ssl"]
        )

        action = "completed"

        async with connection.cursor() as cursor:
            query = """
            INSERT INTO tb_stat (assistant_id, interaction_id, call_id, action, phone_number)
            VALUES (%s, %s, %s, %s, %s)
            """

            await cursor.execute(query, (assistant_id, interaction_id, call_id, action, phone_number))

            if cursor.rowcount > 0:
                logger.info(f"MySQL: Successfully saved call data - call_id: {call_id}, interaction_id: {interaction_id}")
                return True
            else:
                logger.warning(f"MySQL: No rows affected for call_id: {call_id}")
                return False

    except aiomysql.Error as e:
        logger.error(f"MySQL Error saving call {call_id}: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error saving call {call_id}: {e}")
        return False
    finally:
        if connection:
            connection.close()

# ============================================================================
# AUDIO PROCESSING
# ============================================================================

class AudioProcessor:
    """Handle audio format conversions for telephony integration"""

    @staticmethod
    def mulaw_to_pcm(mulaw_data: bytes) -> bytes:
        """Convert μ-law to PCM (Talkdesk → Pipecat)"""
        try:
            return audioop.ulaw2lin(mulaw_data, 2)
        except Exception as e:
            logger.error(f"Error converting μ-law → PCM: {e}")
            return b''

    @staticmethod
    def pcm_to_mulaw(pcm_data: bytes) -> bytes:
        """Convert PCM to μ-law (Pipecat → Talkdesk)"""
        try:
            return audioop.lin2ulaw(pcm_data, 2)
        except Exception as e:
            logger.error(f"Error converting PCM → μ-law: {e}")
            return b''

    @staticmethod
    def resample(audio_data: bytes, from_rate: int, to_rate: int,
                 channels: int = 1, sample_width: int = 2) -> bytes:
        """
        Resample audio between different sample rates.

        Args:
            audio_data: Raw audio bytes
            from_rate: Source sample rate (e.g., 8000)
            to_rate: Target sample rate (e.g., 16000)
            channels: Number of audio channels (default: 1)
            sample_width: Bytes per sample (default: 2 for 16-bit)

        Returns:
            Resampled audio bytes
        """
        try:
            if from_rate == to_rate:
                return audio_data

            resampled, _ = audioop.ratecv(
                audio_data,
                sample_width,
                channels,
                from_rate,
                to_rate,
                None
            )
            return resampled
        except Exception as e:
            logger.error(f"Error resampling {from_rate}Hz → {to_rate}Hz: {e}")
            return audio_data

# ============================================================================
# PIPECAT CONNECTION
# ============================================================================

class PipecatConnection:
    """Manages WebSocket connection with Pipecat AI agent"""

    def __init__(self, config: BridgeConfig):
        self.config = config
        self.websocket: Optional[websockets.WebSocketClientProtocol] = None
        self.call_id: Optional[str] = None
        self.websocket_url: Optional[str] = None
        self.state = ConnectionState.INIT
        self.session_data: Dict[str, Any] = {}

    async def create_connection(self, business_status: str = "close", caller_phone: str = "") -> Dict[str, Any]:
        """
        Prepare Pipecat WebSocket connection.

        Args:
            business_status: "open" or "close" (affects conversation routing)
            caller_phone: Phone number from Talkdesk

        Returns:
            Session metadata dictionary
        """
        try:
            self.state = ConnectionState.CONNECTING

            # Generate unique call ID
            self.call_id = str(uuid.uuid4())

            # Build WebSocket URL with parameters
            from urllib.parse import quote
            encoded_phone = quote(caller_phone) if caller_phone else ""
            ws_url = f"{self.config.pipecat_server_url}?business_status={business_status}&session_id={self.call_id}&caller_phone={encoded_phone}"
            self.websocket_url = ws_url

            logger.info(f"Creating Pipecat connection with business_status: {business_status}")

            # Save session data
            self.session_data = {
                'id': self.call_id,
                'business_status': business_status,
                'created_at': time.time()
            }

            return self.session_data

        except Exception as e:
            self.state = ConnectionState.ERROR
            logger.error(f"Failed to prepare Pipecat connection: {e}")
            raise

    async def connect(self):
        """Establish WebSocket connection to Pipecat"""
        if not self.websocket_url:
            raise ValueError("No WebSocket URL available")

        try:
            logger.info(f"Connecting to Pipecat server: {self.websocket_url}")
            self.websocket = await websockets.connect(
                self.websocket_url,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=10
            )
            self.state = ConnectionState.CONNECTED
            logger.info(f"Connected to Pipecat WebSocket: {self.call_id}")

        except Exception as e:
            self.state = ConnectionState.ERROR
            logger.error(f"Failed to connect to Pipecat: {e}")
            raise

    async def send_audio(self, pcm_data: bytes):
        """Send PCM audio to Pipecat"""
        if self.websocket and self.state == ConnectionState.CONNECTED:
            try:
                await self.websocket.send(pcm_data)
            except Exception as e:
                logger.error(f"Error sending audio to Pipecat: {e}")
                self.state = ConnectionState.ERROR
                raise

    async def receive(self) -> bytes:
        """Receive audio data from Pipecat"""
        if self.websocket and self.state == ConnectionState.CONNECTED:
            data = await self.websocket.recv()
            if isinstance(data, bytes):
                return data
            else:
                # Handle JSON control messages
                try:
                    control_msg = json.loads(data) if isinstance(data, str) else data
                    logger.debug(f"Pipecat control message: {control_msg}")
                    return b''
                except:
                    return b''
        raise ConnectionError("Not connected to Pipecat")

    async def close(self):
        """Close Pipecat WebSocket connection"""
        self.state = ConnectionState.CLOSING
        if self.websocket:
            try:
                await self.websocket.close()
                logger.info(f"Pipecat connection closed: {self.call_id}")
            except Exception as e:
                logger.error(f"Error closing Pipecat connection: {e}")
        self.state = ConnectionState.CLOSED

# ============================================================================
# BRIDGE SESSION
# ============================================================================

class BridgeSession:
    """
    Manages a single call session bridging Talkdesk and Pipecat.

    Responsibilities:
    - Audio forwarding in both directions
    - Format conversion and resampling
    - Session state management
    - Business hours detection
    - Call escalation handling
    """

    def __init__(self, session_id: str, talkdesk_ws: WebSocket, config: BridgeConfig):
        self.session_id = session_id
        self.talkdesk_ws = talkdesk_ws
        self.config = config
        self.pipecat_conn = PipecatConnection(config)
        self.audio_processor = AudioProcessor()
        self.is_active = False
        self.tasks: Set[asyncio.Task] = set()

        self.bridge_state = BridgeState.WAITING_START
        self.escalation_event = asyncio.Event()

        self.stream_sid = None
        self.chunk_counter = 0
        self.interaction_id = None
        self.caller_id = None
        self.business_status = None

        # Audio buffer for messages received before Pipecat is ready
        self.audio_buffer = []

        self.stats = {
            'talkdesk_to_pipecat_packets': 0,
            'pipecat_to_talkdesk_packets': 0,
            'errors': 0
        }

    def extract_business_status(self, business_hours_string: str) -> str:
        """
        Extract business status (open/close) from Talkdesk custom parameters.

        Expected format: "data::data::data::open" or "data::data::data::close"
        """
        try:
            if business_hours_string and '::' in business_hours_string:
                parts = business_hours_string.split('::')
                if len(parts) >= 4:
                    status = parts[-1].strip().lower()
                    logger.info(f"Session {self.session_id}: Extracted business status: {status}")
                    return status

            logger.warning(f"Session {self.session_id}: Could not extract business status")
            return "close"

        except Exception as e:
            logger.error(f"Session {self.session_id}: Error extracting business status: {e}")
            return "close"

    async def initialize_pipecat_with_business_status(self, business_status: str):
        """Initialize Pipecat connection with business hours context"""
        try:
            logger.info(f"Session {self.session_id}: Initializing Pipecat with business_status: {business_status}")

            # Create and connect to Pipecat
            await self.pipecat_conn.create_connection(business_status, self.caller_id or "")
            await self.pipecat_conn.connect()

            # Update bridge state
            self.set_bridge_state(BridgeState.ACTIVE)

            logger.info(f"Session {self.session_id}: Pipecat initialized successfully")

            # Send buffered audio if any
            if self.audio_buffer:
                logger.info(f"Session {self.session_id}: Sending {len(self.audio_buffer)} buffered audio packets")
                for audio_data in self.audio_buffer:
                    try:
                        await self.pipecat_conn.send_audio(audio_data)
                        self.stats['talkdesk_to_pipecat_packets'] += 1
                    except Exception as e:
                        logger.error(f"Error sending buffered audio: {e}")
                        break
                self.audio_buffer.clear()

            return True

        except Exception as e:
            logger.error(f"Session {self.session_id}: Failed to initialize Pipecat: {e}")
            return False

    def set_bridge_state(self, new_state: BridgeState):
        """Update bridge state with logging"""
        old_state = self.bridge_state
        self.bridge_state = new_state
        logger.info(f"Session {self.session_id}: Bridge state changed {old_state.value} → {new_state.value}")

    async def start_escalation(self) -> bool:
        """Start escalation process (transfer to human operator)"""
        try:
            if self.bridge_state != BridgeState.ACTIVE:
                logger.warning(f"Session {self.session_id}: Cannot escalate, state is {self.bridge_state.value}")
                return False

            logger.info(f"Session {self.session_id}: Starting escalation")
            self.set_bridge_state(BridgeState.ESCALATING)

            await self.pipecat_conn.close()
            logger.info(f"Session {self.session_id}: Pipecat closed for escalation")

            self.escalation_event.set()
            await asyncio.sleep(2)

            self.set_bridge_state(BridgeState.PIPECAT_CLOSED)
            logger.info(f"Session {self.session_id}: Ready for escalation")

            return True

        except Exception as e:
            logger.error(f"Session {self.session_id}: Escalation error: {e}")
            return False

    async def start(self):
        """Start bridge session"""
        try:
            logger.info(f"Starting bridge session: {self.session_id}")

            self.is_active = True
            self.set_bridge_state(BridgeState.WAITING_START)

            # Start forwarding tasks
            forward_task = asyncio.create_task(self._forward_talkdesk_to_pipecat())
            backward_task = None

            self.tasks = {forward_task}

            while self.is_active and self.bridge_state not in [BridgeState.CLOSING, BridgeState.CLOSED]:
                # Start Pipecat→Talkdesk forwarding after Pipecat is initialized
                if self.bridge_state == BridgeState.ACTIVE and backward_task is None:
                    backward_task = asyncio.create_task(self._forward_pipecat_to_talkdesk())
                    self.tasks.add(backward_task)
                    logger.info(f"Session {self.session_id}: Bidirectional forwarding active")

                done, pending = await asyncio.wait(
                    self.tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=1.0
                )

                if done and self.bridge_state == BridgeState.ACTIVE:
                    logger.info(f"Session {self.session_id}: Task completed normally")
                    break

            # Cancel remaining tasks
            for task in self.tasks:
                if not task.done():
                    task.cancel()

        except Exception as e:
            logger.error(f"Session {self.session_id} error: {e}")
            self.stats['errors'] += 1
        finally:
            await self.stop()

    async def _forward_talkdesk_to_pipecat(self):
        """Forward audio from Talkdesk to Pipecat"""
        logger.info(f"Session {self.session_id}: Starting Talkdesk → Pipecat forwarding")

        try:
            while self.is_active:
                message = await self.talkdesk_ws.receive_text()

                try:
                    data = json.loads(message)
                    event = data.get('event')

                    if event == 'start':
                        # Extract session metadata
                        self.stream_sid = data.get('streamSid') or data.get('start', {}).get('streamSid')
                        self.interaction_id = data.get('start', {}).get('customParameters', {}).get('interaction_id')
                        self.caller_id = data.get('start', {}).get('customParameters', {}).get('caller_id', '')

                        # Extract business hours
                        business_hours = data.get('start', {}).get('customParameters', {}).get('business_hours', '')
                        self.business_status = self.extract_business_status(business_hours)

                        # Initialize Pipecat with business context
                        await self.initialize_pipecat_with_business_status(self.business_status)

                        # Store in Redis
                        if self.pipecat_conn.call_id:
                            redis_client.hset(
                                self.pipecat_conn.call_id,
                                mapping={
                                    "interaction_id": self.interaction_id,
                                    "stream_sid": self.stream_sid,
                                    "caller_id": self.caller_id
                                }
                            )

                        ACTIVE_SESSIONS[self.stream_sid] = self

                    elif event == 'stop':
                        logger.info(f"Session {self.session_id}: Received STOP from Talkdesk")

                        # Save call to MySQL
                        await save_call_to_mysql(
                            call_id=self.pipecat_conn.call_id,
                            assistant_id=self.config.pipecat_assistant_id,
                            interaction_id=self.interaction_id,
                            phone_number=self.caller_id
                        )

                        break

                    elif event == 'media':
                        # Handle audio data
                        media = data.get('media', {})
                        if media.get('track') == 'inbound':
                            payload = media.get('payload', '')

                            # Decode and convert audio
                            mulaw_data = base64.b64decode(payload)
                            pcm_8khz = self.audio_processor.mulaw_to_pcm(mulaw_data)
                            pcm_16khz = self.audio_processor.resample(
                                pcm_8khz,
                                self.config.talkdesk_sample_rate,
                                self.config.pipecat_sample_rate
                            )

                            if self.bridge_state == BridgeState.WAITING_START:
                                # Buffer audio until Pipecat is ready
                                self.audio_buffer.append(pcm_16khz)
                                if len(self.audio_buffer) > 100:
                                    self.audio_buffer.pop(0)

                            elif self.bridge_state == BridgeState.ACTIVE:
                                # Forward audio to Pipecat
                                try:
                                    await self.pipecat_conn.send_audio(pcm_16khz)
                                    self.stats['talkdesk_to_pipecat_packets'] += 1
                                except Exception:
                                    pass

                except json.JSONDecodeError:
                    logger.error(f"Invalid JSON from Talkdesk")
                except Exception as e:
                    logger.error(f"Error processing Talkdesk message: {e}")
                    self.stats['errors'] += 1

        except Exception as e:
            logger.error(f"Session {self.session_id}: Forward error: {e}")
            self.stats['errors'] += 1

    async def _forward_pipecat_to_talkdesk(self):
        """Forward audio from Pipecat to Talkdesk"""
        logger.info(f"Session {self.session_id}: Starting Pipecat → Talkdesk forwarding")

        try:
            while self.is_active and self.bridge_state == BridgeState.ACTIVE:
                try:
                    data = await self.pipecat_conn.receive()

                    if isinstance(data, bytes) and len(data) > 0:
                        # Convert and resample audio
                        pcm_16khz = data
                        pcm_8khz = self.audio_processor.resample(
                            pcm_16khz,
                            self.config.pipecat_sample_rate,
                            self.config.talkdesk_sample_rate
                        )

                        # Convert to μ-law
                        mulaw_data = self.audio_processor.pcm_to_mulaw(pcm_8khz)
                        payload = base64.b64encode(mulaw_data).decode()

                        self.chunk_counter += 1

                        # Build Talkdesk message
                        message = {
                            "event": "media",
                            "streamSid": self.stream_sid,
                            "media": {
                                "track": "outbound",
                                "chunk": str(self.chunk_counter),
                                "timestamp": str(int(time.time() * 1000)),
                                "payload": payload
                            }
                        }

                        await self.talkdesk_ws.send_text(json.dumps(message))
                        self.stats['pipecat_to_talkdesk_packets'] += 1

                except ConnectionError:
                    logger.error(f"Pipecat connection lost")
                    break
                except Exception as e:
                    logger.error(f"Backward error: {e}")
                    self.stats['errors'] += 1
                    break

        except Exception as e:
            logger.error(f"Fatal backward error: {e}")
            self.stats['errors'] += 1

    async def stop(self):
        """Stop bridge session and cleanup"""
        logger.info(f"Stopping session {self.session_id}")
        self.is_active = False
        self.set_bridge_state(BridgeState.CLOSED)

        logger.info(f"Session stats: "
                   f"Talkdesk→Pipecat: {self.stats['talkdesk_to_pipecat_packets']}, "
                   f"Pipecat→Talkdesk: {self.stats['pipecat_to_talkdesk_packets']}, "
                   f"Errors: {self.stats['errors']}")

        if self.pipecat_conn.state not in [ConnectionState.CLOSED, ConnectionState.CLOSING]:
            await self.pipecat_conn.close()

# ============================================================================
# FASTAPI APPLICATION
# ============================================================================

app = FastAPI()

@app.get("/healthz")
async def healthz():
    """Health check endpoint"""
    return {"status": "ok", "service": "pipecat-bridge"}

@app.websocket("/talkdesk")
async def talkdesk_ws(ws: WebSocket):
    """
    Main WebSocket endpoint for Talkdesk connections.

    This endpoint receives calls from Talkdesk and bridges them to Pipecat.
    """
    await ws.accept()
    session_id = str(uuid.uuid4())
    logger.info(f"New Talkdesk connection – Session: {session_id}")
    session = BridgeSession(session_id, ws, cfg)
    ACTIVE_SESSIONS[session_id] = session

    try:
        await session.start()
    except WebSocketDisconnect:
        logger.info(f"Session {session_id} disconnected")
    finally:
        ACTIVE_SESSIONS.pop(session_id, None)
        logger.info(f"Session {session_id} ended")

@app.post("/escalation")
async def escalation(request: Request) -> Dict[str, Any]:
    """
    Handle call escalation requests from Pipecat.

    When the AI agent determines a call should be transferred to a human,
    it sends a request to this endpoint.
    """
    payload = await request.json()
    call_id = payload.get("message", {}).get("call", {}).get("id")

    # Retrieve session from Redis
    if call_id:
        stream_sid = redis_client.hget(call_id, "stream_sid")
        if isinstance(stream_sid, bytes):
            stream_sid = stream_sid.decode()

        if stream_sid:
            session = ACTIVE_SESSIONS.get(stream_sid)
            if session:
                try:
                    # Start escalation process
                    await session.start_escalation()
                    logger.info(f"Escalation completed for {stream_sid}")
                except Exception as e:
                    logger.error(f"Escalation error: {e}")

    return {"results": [{"result": call_id or "Error"}]}

# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)

"""
DEPLOYMENT NOTES:
================

1. Environment Variables Required:
   - MYSQL_HOST: MySQL database host
   - MYSQL_PORT: MySQL port (default: 3306)
   - MYSQL_USER: Database username
   - MYSQL_PASSWORD: Database password
   - MYSQL_DATABASE: Database name
   - REDIS_HOST: Redis cache host
   - REDIS_PORT: Redis port (default: 6380)
   - REDIS_PASSWORD: Redis password
   - PIPECAT_SERVER_URL: WebSocket URL of Pipecat agent
   - PIPECAT_ASSISTANT_ID: Agent identifier
   - PORT: Bridge server port (default: 8080)

2. Audio Processing:
   - Talkdesk: μ-law 8kHz
   - Pipecat: PCM 16-bit 16kHz
   - Automatic conversion and resampling

3. Session Management:
   - Redis: Temporary session data
   - MySQL: Permanent call records
   - Automatic cleanup after calls

4. Integration Points:
   - Talkdesk WebSocket: /talkdesk
   - Pipecat WebSocket: PIPECAT_SERVER_URL
   - Escalation API: /escalation

For full documentation, see README.md
"""
