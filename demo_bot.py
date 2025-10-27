"""
DEMO VERSION - Healthcare Voice Booking Agent
==============================================

This is a sanitized version of the production bot.py for demonstration purposes.
All sensitive credentials have been removed and replaced with environment variables.

ARCHITECTURE OVERVIEW:
- FastAPI WebSocket transport for production telephony integration
- Pipecat pipeline: STT → LLM → TTS
- Intelligent conversation flows using pipecat-flows
- Healthcare service API integration
- Comprehensive logging and transcript recording

PRODUCTION SETUP:
- Hosted on Azure VM with Docker
- Integrated with Talkdesk telephony via bridge service
- Uses Deepgram (STT), OpenAI (LLM), ElevenLabs (TTS)
- MySQL for call logging, Redis for session caching
"""

import os
import asyncio
import logging
from typing import Dict, Any
from dotenv import load_dotenv
from loguru import logger

# FastAPI
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# Core Pipecat imports
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    OutputAudioRawFrame
)
from pipecat.processors.transcript_processor import TranscriptProcessor
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from pipecat.audio.vad.silero import SileroVADAnalyzer, VADParams
from pipecat.serializers.base_serializer import FrameSerializer, FrameSerializerType

# Flow management (custom implementation)
from flows.manager import create_flow_manager, initialize_flow_manager

# Service components (custom factories)
from pipeline.components import (
    create_stt_service,
    create_tts_service,
    create_llm_service,
    create_context_aggregator
)

# Utilities
from services.transcript_manager import get_transcript_manager, cleanup_transcript_manager
from services.idle_handler import create_user_idle_processor
from services.call_logger import CallLogger
from config.settings import settings

load_dotenv(override=True)

# ============================================================================
# AUDIO SERIALIZER
# ============================================================================

class RawPCMSerializer(FrameSerializer):
    """
    Handles PCM raw audio serialization for telephony integration.

    - Input: PCM signed 16-bit little-endian, 16kHz, mono (from bridge)
    - Output: PCM signed 16-bit little-endian, 16kHz, mono (to bridge)
    """

    @property
    def type(self):
        return FrameSerializerType.BINARY

    async def serialize(self, frame: Frame) -> bytes:
        """Serialize outgoing audio frames to raw PCM"""
        if isinstance(frame, OutputAudioRawFrame):
            return frame.audio
        return b''

    async def deserialize(self, data) -> Frame:
        """Deserialize incoming raw PCM to audio frames"""
        if isinstance(data, bytes) and len(data) > 0:
            return InputAudioRawFrame(
                audio=data,
                sample_rate=16000,
                num_channels=1
            )
        return None

# ============================================================================
# FASTAPI APPLICATION
# ============================================================================

app = FastAPI(
    title="Healthcare Voice Booking Agent",
    description="AI-powered appointment booking system with intelligent conversation flows",
    version="1.0.0"
)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Store for active sessions
active_sessions: Dict[str, Any] = {}

# ============================================================================
# API ENDPOINTS
# ============================================================================

@app.get("/")
async def root():
    """Homepage with server information"""
    return HTMLResponse(f"""
    <html>
        <head>
            <title>Healthcare Voice Booking Agent</title>
            <style>
                body {{
                    font-family: 'Segoe UI', Arial, sans-serif;
                    margin: 40px;
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    color: white;
                }}
                .container {{
                    background: rgba(255,255,255,0.95);
                    color: #333;
                    padding: 30px;
                    border-radius: 10px;
                    box-shadow: 0 10px 40px rgba(0,0,0,0.2);
                    max-width: 800px;
                    margin: 0 auto;
                }}
                .status {{
                    color: #22c55e;
                    font-weight: bold;
                }}
                .service {{
                    display: inline-block;
                    padding: 5px 10px;
                    margin: 5px;
                    background: #667eea;
                    color: white;
                    border-radius: 5px;
                    font-size: 12px;
                }}
                h1 {{ color: #333; }}
                h2 {{ color: #667eea; margin-top: 30px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>🏥 Healthcare Voice Booking Agent</h1>
                <p class="status">✅ Server is running</p>

                <h2>Active Services:</h2>
                <div>
                    <span class="service">Deepgram STT</span>
                    <span class="service">OpenAI GPT-4</span>
                    <span class="service">ElevenLabs TTS</span>
                    <span class="service">Pipecat Flows</span>
                </div>

                <h2>Endpoints:</h2>
                <ul>
                    <li><code>GET /</code> - This page</li>
                    <li><code>GET /health</code> - Health check</li>
                    <li><code>WS /ws</code> - WebSocket endpoint for voice calls</li>
                </ul>

                <h2>Statistics:</h2>
                <p>Active sessions: <strong>{len(active_sessions)}</strong></p>
            </div>
        </body>
    </html>
    """)

@app.get("/health")
async def health_check():
    """Health check endpoint for monitoring"""
    return JSONResponse({
        "status": "healthy",
        "service": "healthcare-voice-booking-agent",
        "version": "1.0.0",
        "active_sessions": len(active_sessions),
        "services": {
            "stt": "deepgram",
            "llm": "openai-gpt4",
            "tts": "elevenlabs",
            "flows": "pipecat-flows",
            "transport": "fastapi-websocket"
        }
    })

# ============================================================================
# MAIN WEBSOCKET ENDPOINT
# ============================================================================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    Main WebSocket endpoint for healthcare voice conversations.

    Query Parameters:
    - business_status: "open" or "close" (affects conversation routing)
    - session_id: Unique identifier for the call
    - start_node: Starting conversation node (default: "greeting")
    - caller_phone: Phone number from telephony system

    Audio Format:
    - Input/Output: PCM signed 16-bit LE, 16kHz, mono
    """
    await websocket.accept()

    # Extract parameters from query string
    query_params = dict(websocket.query_params)
    business_status = query_params.get("business_status", "open")
    import uuid
    session_id = query_params.get("session_id", f"session-{uuid.uuid4().hex[:8]}")
    start_node = query_params.get("start_node", "greeting")
    caller_phone = query_params.get("caller_phone", "")

    logger.info(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info(f"New WebSocket Connection")
    logger.info(f"Session ID: {session_id}")
    logger.info(f"Business Status: {business_status}")
    logger.info(f"Start Node: {start_node}")
    logger.info(f"Caller Phone: {caller_phone or 'Not provided'}")
    logger.info(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    # Pipeline components
    runner = None
    task = None

    try:
        # ====================================================================
        # ENVIRONMENT VALIDATION
        # ====================================================================

        required_keys = [
            ("DEEPGRAM_API_KEY", "Deepgram"),
            ("ELEVENLABS_API_KEY", "ElevenLabs"),
            ("OPENAI_API_KEY", "OpenAI")
        ]

        for key_name, service_name in required_keys:
            if not os.getenv(key_name):
                raise Exception(f"{key_name} not found - required for {service_name}")

        logger.success("✅ All required API keys validated")

        # ====================================================================
        # TRANSPORT CREATION
        # ====================================================================

        transport = FastAPIWebsocketTransport(
            websocket=websocket,
            params=FastAPIWebsocketParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                add_wav_header=False,
                vad_analyzer=SileroVADAnalyzer(
                    params=VADParams(
                        start_secs=settings.vad_config["start_secs"],
                        stop_secs=settings.vad_config["stop_secs"],
                        min_volume=settings.vad_config["min_volume"]
                    )
                ),
                serializer=RawPCMSerializer(),
                session_timeout=900,  # 15 minutes
            )
        )

        logger.info("✅ Transport created with VAD and PCM serialization")

        # ====================================================================
        # AI SERVICES INITIALIZATION
        # ====================================================================

        logger.info("Initializing AI services...")

        stt = create_stt_service()          # Deepgram STT
        tts = create_tts_service()          # ElevenLabs TTS
        llm = create_llm_service()          # OpenAI GPT-4
        context_aggregator = create_context_aggregator(llm)

        logger.info("✅ All AI services initialized")

        # ====================================================================
        # TRANSCRIPT RECORDING SETUP
        # ====================================================================

        transcript_processor = TranscriptProcessor()

        @transcript_processor.event_handler("on_transcript_update")
        async def on_transcript_update(processor, frame):
            """Capture conversation transcript for logging and analysis"""
            session_transcript_manager = get_transcript_manager(session_id)

            for message in frame.messages:
                if message.role == "user":
                    session_transcript_manager.add_user_message(message.content)
                elif message.role == "assistant":
                    session_transcript_manager.add_assistant_message(message.content)

        # ====================================================================
        # IDLE DETECTION
        # ====================================================================

        user_idle_processor = create_user_idle_processor(timeout_seconds=30.0)
        logger.info("✅ Idle detection processor created (30s timeout)")

        # ====================================================================
        # PIPELINE CONSTRUCTION
        # ====================================================================

        pipeline = Pipeline([
            transport.input(),                      # Receive audio from bridge
            stt,                                    # Speech-to-Text
            user_idle_processor,                    # Detect silence/failures
            transcript_processor.user(),            # Record user input
            context_aggregator.user(),              # Build conversation context
            llm,                                    # Generate response
            tts,                                    # Text-to-Speech
            transport.output(),                     # Send audio to bridge
            transcript_processor.assistant(),       # Record assistant output
            context_aggregator.assistant()          # Update context
        ])

        logger.info("✅ Pipeline constructed:")
        logger.info("  Input → STT → Idle → Transcript → Context → LLM → TTS → Output")

        # Create pipeline task
        task = PipelineTask(
            pipeline,
            params=PipelineParams(
                allow_interruptions=True,
                enable_transcriptions=True,
                audio_in_sample_rate=16000,
                audio_out_sample_rate=16000,
            )
        )

        # ====================================================================
        # CALL LOGGING
        # ====================================================================

        session_call_logger = CallLogger(session_id)
        log_file = session_call_logger.start_call_logging(session_id, caller_phone)
        logger.info(f"📁 Call logging started: {log_file}")

        # ====================================================================
        # FLOW MANAGER INITIALIZATION
        # ====================================================================

        flow_manager = create_flow_manager(task, llm, context_aggregator, transport)

        # Store caller phone in flow state
        if caller_phone:
            flow_manager.state["caller_phone_from_talkdesk"] = caller_phone
            logger.info(f"✅ Caller phone stored in flow state: {caller_phone}")

        # Initialize STT switcher for dynamic language switching
        from utils.stt_switcher import initialize_stt_switcher
        initialize_stt_switcher(stt, flow_manager)

        # ====================================================================
        # EVENT HANDLERS
        # ====================================================================

        @transport.event_handler("on_client_connected")
        async def on_client_connected(transport_obj, ws):
            """Handle client connection"""
            logger.info(f"✅ Client connected: {session_id}")

            active_sessions[session_id] = {
                "websocket": ws,
                "business_status": business_status,
                "connected_at": asyncio.get_event_loop().time(),
                "call_logger": session_call_logger,
                "services": {
                    "stt": "deepgram",
                    "llm": "openai-gpt4-flows",
                    "tts": "elevenlabs",
                    "flows": "pipecat-flows"
                }
            }

            # Start transcript recording
            session_transcript_manager = get_transcript_manager(session_id)
            session_transcript_manager.start_session(session_id)
            logger.info(f"📝 Transcript recording started")

            # Initialize conversation flow
            try:
                await initialize_flow_manager(flow_manager, start_node)
                logger.success(f"✅ Flow initialized at node: {start_node}")
            except Exception as e:
                logger.error(f"Error during flow initialization: {e}")

        @transport.event_handler("on_client_disconnected")
        async def on_client_disconnected(transport_obj, ws):
            """Handle client disconnection and cleanup"""
            logger.info(f"🔌 Client disconnected: {session_id}")

            # Extract and store call data
            try:
                session_transcript_manager = get_transcript_manager(session_id)
                success = await session_transcript_manager.extract_and_store_call_data(flow_manager)
                if success:
                    logger.success(f"✅ Call data extracted and stored")
                else:
                    logger.error(f"❌ Failed to extract call data")
            except Exception as e:
                logger.error(f"❌ Error during call data extraction: {e}")

            # Cleanup
            cleanup_transcript_manager(session_id)
            if session_id in active_sessions:
                del active_sessions[session_id]
            await task.cancel()

        @transport.event_handler("on_session_timeout")
        async def on_session_timeout(transport_obj, ws):
            """Handle session timeout"""
            logger.warning(f"⏱️ Session timeout: {session_id}")

            # Extract call data even on timeout
            try:
                session_transcript_manager = get_transcript_manager(session_id)
                await session_transcript_manager.extract_and_store_call_data(flow_manager)
            except Exception as e:
                logger.error(f"Error during timeout data extraction: {e}")

            # Cleanup
            cleanup_transcript_manager(session_id)
            if session_id in active_sessions:
                del active_sessions[session_id]
            await task.cancel()

        # ====================================================================
        # START PIPELINE
        # ====================================================================

        runner = PipelineRunner()
        logger.info(f"🚀 Pipeline started for session: {session_id}")
        logger.info(f"🏥 Intelligent conversation flows ACTIVE")

        # Run pipeline (blocks until disconnection)
        await runner.run(task)

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected: {session_id}")
    except Exception as e:
        logger.error(f"❌ Error in WebSocket handler: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # Final cleanup
        if session_id in active_sessions:
            del active_sessions[session_id]

        # Stop call logging
        try:
            saved_log_file = session_call_logger.stop_call_logging()
            if saved_log_file:
                logger.info(f"📁 Call log saved: {saved_log_file}")
        except Exception as e:
            logger.error(f"Error stopping call logging: {e}")

        logger.info(f"Session ended: {session_id}")
        logger.info(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

# ============================================================================
# APPLICATION ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    host = os.environ.get("HOST", "0.0.0.0")

    logger.info(f"Starting Healthcare Voice Booking Agent on {host}:{port}")
    uvicorn.run("demo_bot:app", host=host, port=port, reload=False)

"""
DEPLOYMENT NOTES:
================

1. Environment Variables Required:
   - DEEPGRAM_API_KEY: For speech-to-text
   - ELEVENLABS_API_KEY: For text-to-speech
   - OPENAI_API_KEY: For LLM
   - PORT: Server port (default: 8000)
   - HOST: Server host (default: 0.0.0.0)

2. External Dependencies:
   - flows/: Custom conversation flow management
   - services/: Healthcare API integration, logging, storage
   - pipeline/components.py: AI service factory functions
   - config/settings.py: Centralized configuration

3. Production Integration:
   - Talkdesk telephony → bridge_conn.py → bot.py (this file)
   - Redis for session caching
   - MySQL for call logging
   - Azure Blob Storage for transcripts

4. Docker Deployment:
   - Multi-stage Dockerfile for optimized builds
   - docker-compose.yml for orchestration
   - Automatic health checks and restarts

5. Monitoring:
   - Per-call log files in logs/calls/
   - Centralized application log
   - Health check endpoint at /health
   - Conversation transcripts saved to database

For full documentation, see README.md
"""
