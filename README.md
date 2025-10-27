# Healthcare Voice Booking Agent - Pipecat Flows

A sophisticated Italian healthcare appointment booking system powered by AI voice technology, built on the Pipecat framework with advanced conversation flows.

## Table of Contents
- [Overview](#overview)
- [System Architecture](#system-architecture)
- [Technology Stack](#technology-stack)
- [Features](#features)
- [Flow Architecture](#flow-architecture)
- [Deployment](#deployment)
- [Development Workflow](#development-workflow)
- [Configuration](#configuration)
- [API Documentation](#api-documentation)
- [Monitoring & Logging](#monitoring--logging)

---

## Overview

This project implements an AI-powered voice agent that handles patient calls for Italian healthcare facilities. The system manages complete appointment booking workflows through natural voice conversations in Italian, including:

- Patient information collection (name, phone, email, fiscal code)
- Healthcare service selection with intelligent matching
- Appointment scheduling with availability checking
- Call escalation to human operators when needed
- Comprehensive conversation logging and analytics

### Key Capabilities
- **Multi-language Support**: Italian (production) and English (testing)
- **Natural Conversations**: Advanced flow management for contextual dialogues
- **Healthcare Integration**: Real-time service matching and availability checking
- **Production-Ready**: Azure-hosted with full monitoring and logging
- **Flexible Testing**: Voice and text-based testing modes for rapid development

---

## System Architecture

### High-Level Flow
Calls comes from talk desk -> goes to bridge_Conn.py -> main agent


### Component Breakdown

#### 1. **Bridge Service** (`hosted on azure`)
- **Purpose**: Connects Talkdesk telephony platform with Pipecat agent
- **Responsibilities**:
  - Audio format conversion (μ-law 8kHz ↔ PCM 16kHz)
  - WebSocket bidirectional forwarding
  - Business hours detection and routing
  - Session management and Redis caching
  - Call metadata extraction (caller ID, interaction ID)

#### 2. **Main Agent** (`hosted on azure`)
- **Purpose**: Core AI conversation engine
- **Responsibilities**:
  - Pipecat pipeline management (STT → LLM → TTS)
  - Conversation flow orchestration
  - Healthcare service API integration
  - Transcript recording and call data extraction
  - Session timeout handling


---

## Technology Stack

### Core Framework
| Component | Technology | Version | Purpose |
|-----------|-----------|---------|---------|
| **Conversation AI** | Pipecat | Latest | Voice pipeline orchestration |
| **Transport** | FastAPI WebSocket | - | Production transport layer |
| **Testing Transport** | Daily WebRTC | - | Development testing |
| **Language** | Python | 3.11+ | Core programming language |

### AI Services
| Service | Provider | Model | Configuration |
|---------|----------|-------|---------------|
| **STT** | Deepgram | nova-3-general | Italian (`it`) / English (`en`) |
| **LLM** | OpenAI | GPT-4.1-mini | Function calling enabled |
| **TTS** | ElevenLabs | eleven_multilingual_v2 | Voice ID: `gfKKsLN1k0oYYN9n2dXX` |
| **VAD** | Silero | - | Voice activity detection |

### Infrastructure
| Service | Platform | Purpose |
|---------|----------|---------|
| **Compute** | Azure VM | Production hosting |
| **Database** | Azure MySQL | Call logs (2-month retention) |
| **Cache** | Azure Redis | Session data |
| **Storage** | Azure Blob | Transcripts and recordings |
| **Registry** | Docker Hub | Container images |

---

## Features

### Intelligent Conversation Flows
- **Dynamic routing** based on business hours (open/closed)
- **Context-aware** patient information collection
- **Fuzzy matching** for healthcare service search
- **Multi-turn dialogues** with state persistence
- **Intelligent fallbacks** for API failures

### Patient Data Management
- **Name validation** with gender detection (italiano/italiana)
- **Phone number formatting** (Italian mobile format)
- **Email validation** and confirmation
- **Fiscal code validation** (Italian codice fiscale)
- **GDPR-compliant** data retention policies

### Healthcare Service Integration
- **Real-time service matching** via external API
- **Natural language processing** for service requests
- **Top-3 suggestions** with patient selection
- **Availability checking** for selected services
- **Appointment slot presentation**

### Production Features
- **Call escalation** to human operators
- **Comprehensive logging** (per-call files + centralized logs)
- **Transcript recording** for quality assurance
- **Session timeout handling** (15 minutes)
- **Health monitoring** endpoints
- **Automatic data cleanup** (2-month retention)

---

## Flow Architecture

Talk desk -> Bridge_conn.py (hosted on azure) -> main booking agent (hosted on azure vm)

---

## Deployment

### Current Deployment Process

#### Prerequisites
- Azure VM with Docker installed
- Docker Hub account with push access
- Environment variables configured on VM

#### Step-by-Step Deployment

1. **Local Development & Testing**
   ```bash
   # Fast text-based testing (recommended for development)
   python chat_test.py                    # Full conversation flow
   python chat_test.py --start-node email # Test specific node

   # Voice testing (final validation)
   python test.py                         # Full flow with voice
   python test.py --start-node booking    # Test specific node with voice
   ```

2. **Code Changes**
   ```bash
   # Make changes to bot.py, flows/, or services/
   git add .
   git commit -m "Your change description"
   git push origin main
   ```

3. **Build Docker Image**
   ```bash
   # Build locally
   docker build -t ...... .

   # Push to Docker Hub
   docker push ..../....:latest
   ```

4. **Deploy to Azure VM**
   ```bash
   # SSH into Azure VM
   ssh user@your-azure-vm

   # Navigate to project directory
   cd /path/to/project

   # Stop current container
   docker-compose down

   # Remove old image
   docker image rm rudyimhtpdev/voicebooking_piemo1:latest

   # Pull latest image
   docker-compose pull

   # Start new container
   docker-compose up -d

   # Clean up old images
   docker image prune -f

   # Check logs
   docker-compose logs -f pipecat-agent
   ```


## Project Structure

```
pipecat-flows-italian/
├── bot.py                          # Main production agent
├── test.py                         # Voice testing with Daily
├── chat_test.py                    # Text-only testing (fast)
├── Dockerfile                      # Multi-stage Docker build
├── docker-compose.yml              # Production deployment config
├── requirements.txt                # Python dependencies
├── requirements-base.txt           # Stable base dependencies
├── .env                            # Environment variables (not in git)
│
├── talkdeskbridge/
│   └── bridge_conn.py              # Talkdesk audio bridge service
│
├── flows/
│   ├── manager.py                  # Flow initialization
│   ├── nodes/                      # Conversation node definitions
│   │   ├── base.py                 # Base node creator
│   │   ├── greeting.py             # Welcome flow
│   │   ├── name.py                 # Name collection
│   │   ├── phone.py                # Phone validation
│   │   ├── email.py                # Email collection
│   │   ├── fiscal_code.py          # Fiscal code validation
│   │   ├── booking.py              # Service selection
│   │   └── slot_selection.py       # Appointment booking
│   └── handlers/                   # Flow event handlers
│       ├── patient_detail_handlers.py
│       └── booking_handlers.py
│
├── config/
│   └── settings.py                 # Centralized configuration
│
├── services/
│   ├── config.py                   # Healthcare API config
│   ├── healthcare_api.py           # Service matching API
│   ├── transcript_manager.py       # Conversation recording
│   ├── call_logger.py              # Per-call logging
│   ├── call_storage.py             # Azure storage integration
│   └── idle_handler.py             # Silence detection
│
├── pipeline/
│   └── components.py               # Pipecat service factories
│
├── utils/
│   ├── stt_switcher.py             # Dynamic STT configuration
│   └── validators.py               # Input validation helpers
│
└── logs/
    ├── app.log                     # Main application log
    └── calls/                      # Per-call log files
```

