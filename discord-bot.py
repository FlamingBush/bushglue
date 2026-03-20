#!/usr/bin/env python3
"""
Discord bot for the Bush Glue pipeline.

Registers a /pray slash command that runs the full audio injection pipeline.
The bot responds in #ai-design and in DMs.

Setup:
  1. Create a Discord application at https://discord.com/developers/applications
  2. Add a bot, copy the token
  3. echo "DISCORD_TOKEN=<token>" > /home/ubuntu/.config/bush/discord-token.env
  4. Enable MESSAGE CONTENT intent on the Bot tab
  5. Invite the bot with scopes: bot, applications.commands
     Permissions: Send Messages, Embed Links, Add Reactions, Read Message History,
                  Connect, Speak (for voice channel support)
  6. systemctl enable --now bush-discord

Environment variables:
  DISCORD_TOKEN   Bot token (required)
  DISCORD_GUILD_ID  Guild (server) ID for fast slash command sync (optional)
  MQTT_BROKER     Override MQTT broker host (default: auto-detect via bushutil)
"""
import asyncio
import io
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time

logging.basicConfig(level=logging.WARNING, format="[%(name)s] %(levelname)s %(message)s")
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import discord
from discord import app_commands
import numpy as np
import paho.mqtt.client as mqtt

# Phase 2: discord-ext-voice-recv (optional — graceful degradation if absent)
try:
    from discord.ext import voice_recv
    HAS_VOICE_RECV = True
except ImportError:
    HAS_VOICE_RECV = False

# ── MQTT topics ───────────────────────────────────────────────────────────────
TOPIC_TRANSCRIPT = "bush/pipeline/stt/transcript"
TOPIC_VERSE      = "bush/pipeline/t2v/verse"
TOPIC_SPEAKING   = "bush/pipeline/tts/speaking"
TOPIC_DONE       = "bush/pipeline/tts/done"
TOPIC_SENTIMENT  = "bush/pipeline/sentiment/result"
TOPIC_FLARE      = "bush/flame/flare/pulse"
TOPIC_BIGJET     = "bush/flame/bigjet/pulse"

MQTT_PORT   = 1883
REPO_DIR = Path(__file__).parent

# ── pipeline timeouts (seconds) ───────────────────────────────────────────────
T_TRANSCRIPT = 30
T_VERSE      = 45
T_DONE       = 90   # from transcript time

# ── embed appearance ──────────────────────────────────────────────────────────
EMOTION_COLORS = {
    "anger":   0xE74C3C,  # red
    "joy":     0x2ECC71,  # green
    "love":    0xFF69B4,  # pink
    "surprise":0xF1C40F,  # yellow
    "fear":    0xE67E22,  # orange
    "sadness": 0x3498DB,  # blue
}
EMOTIONS_ORDER = ["anger", "joy", "love", "surprise", "fear", "sadness"]
BAR_WIDTH = 10

# ── TTS synthesis params (mirrors tts-service.py) ────────────────────────────
ESPEAK_CMD  = ["espeak-ng", "-v", "en-gb", "-s", "95", "-p", "1", "-a", "200", "--stdout"]
SOX_EFFECTS = ["gain", "-8", "pitch", "-250", "reverb", "65", "12", "100", "100", "28", "3"]


# ── data class ────────────────────────────────────────────────────────────────

@dataclass
class PipelineResult:
    verse: Optional[str]
    transcript: Optional[str]
    sentiment: Optional[dict]       # raw classification list from MQTT payload
    stages: list                    # list of (name, status, elapsed_s, timeout_s)
    flare_count: int
    flare_total_ms: int
    bigjet_count: int
    bigjet_total_ms: int
    total_elapsed_s: float
    passed: bool


# ── Discord TTS audio source (Phase 1) ───────────────────────────────────────

class DiscordTTSSource(discord.AudioSource):
    """
    Long-lived AudioSource that synthesizes verses and streams 48kHz stereo PCM
    to Discord VC. Returns silence frames when idle so voice_client.play()
    never stops.
    """
    FRAME_SIZE = 3840  # 20ms @ 48kHz stereo s16le

    def __init__(self):
        self._buf     = b""
        self._buf_pos = 0
        self._lock    = threading.Lock()

    def read(self) -> bytes:
        with self._lock:
            remaining = len(self._buf) - self._buf_pos
            if remaining >= self.FRAME_SIZE:
                start = self._buf_pos
                self._buf_pos += self.FRAME_SIZE
                return self._buf[start:start + self.FRAME_SIZE]
            elif remaining > 0:
                frame = self._buf[self._buf_pos:]
                self._buf     = b""
                self._buf_pos = 0
                return frame + b'\x00' * (self.FRAME_SIZE - len(frame))
        return b""  # empty → signals discord.py to stop play(), bot shows as quiet

    def is_opus(self) -> bool:
        return False

    def load(self, pcm: bytes):
        """Load synthesized PCM into the buffer for playback."""
        with self._lock:
            self._buf     = pcm
            self._buf_pos = 0

    async def synthesize(self, text: str) -> bytes:
        """Synthesize verse text to raw PCM bytes (does not start playback)."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._run_synth, text)

    def _run_synth(self, text: str) -> bytes:
        try:
            espeak = subprocess.Popen(
                ESPEAK_CMD + [text],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            sox = subprocess.Popen(
                [
                    "sox", "-q", "-t", "wav", "-",
                    "-t", "raw", "-r", "48000", "-c", "2",
                    "-e", "signed-integer", "-b", "16", "-",
                ] + SOX_EFFECTS,
                stdin=espeak.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            espeak.stdout.close()
            pcm, _ = sox.communicate()
            espeak.wait()
            return pcm
        except Exception as e:
            print(f"[tts-source] synth error: {e}", flush=True)
            return b""


# ── Loopback writer for voice receive → STT (Phase 2) ────────────────────────

class LoopbackWriter:
    """
    Receives 48kHz stereo s16le PCM from Discord, downsamples to 16kHz mono,
    and writes it to the ALSA loopback device for the existing stt-service.
    """
    LOOPBACK_DEVICE = 4  # Loopback: PCM (hw:7,0) — playback side; stt-service reads from hw:7,1

    def __init__(self):
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=50)
        self._thread: Optional[threading.Thread] = None
        self._stop  = threading.Event()
        self.muted  = False

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._queue.put(b"")  # unblock blocking get
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None

    def push(self, pcm_48k_stereo: bytes):
        if self.muted or not pcm_48k_stereo:
            return
        try:
            self._queue.put_nowait(pcm_48k_stereo)
        except queue.Full:
            pass  # drop on backpressure

    def _run(self):
        import sounddevice as sd
        try:
            with sd.RawOutputStream(
                samplerate=16000,
                channels=1,
                dtype="int16",
                device=self.LOOPBACK_DEVICE,
            ) as stream:
                while not self._stop.is_set():
                    try:
                        chunk = self._queue.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    if not chunk:
                        break
                    # 48kHz stereo s16le → 16kHz mono
                    arr = np.frombuffer(chunk, dtype=np.int16).reshape(-1, 2)
                    mono_48k = arr.mean(axis=1).astype(np.int16)
                    mono_16k = mono_48k[::3]
                    stream.write(mono_16k.tobytes())
        except Exception as e:
            print(f"[loopback-writer] error: {e}", flush=True)


# ── MQTT bridge ───────────────────────────────────────────────────────────────

class MQTTBridge:
    """
    Long-lived paho-mqtt client that bridges messages into asyncio callbacks.

    Handlers are registered per-topic and called as coroutines on the given
    event loop via asyncio.run_coroutine_threadsafe().
    """

    def __init__(self, broker: str, loop: asyncio.AbstractEventLoop):
        self._broker  = broker
        self._loop    = loop
        self._lock    = threading.Lock()
        self._handlers: dict[str, list] = {}   # topic → [async fn(topic, payload)]
        self._subscribed: set           = set()
        self._connected                 = threading.Event()

        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

    def connect(self) -> bool:
        self._client.connect(self._broker, MQTT_PORT, 60)
        self._client.loop_start()
        return self._connected.wait(timeout=10)

    def disconnect(self):
        self._client.loop_stop()
        self._client.disconnect()

    def publish(self, topic: str, payload):
        self._client.publish(topic, payload)

    def add_handler(self, topic: str, callback):
        with self._lock:
            if topic not in self._handlers:
                self._handlers[topic] = []
            self._handlers[topic].append(callback)
            if topic not in self._subscribed:
                self._client.subscribe(topic)
                self._subscribed.add(topic)

    def remove_handler(self, topic: str, callback):
        with self._lock:
            if topic in self._handlers:
                try:
                    self._handlers[topic].remove(callback)
                except ValueError:
                    pass

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        # Re-subscribe on reconnect
        with self._lock:
            for topic in self._subscribed:
                client.subscribe(topic)
        self._connected.set()
        print(f"[mqtt] Connected to {self._broker} (rc={rc})", flush=True)

    def _on_message(self, client, userdata, msg):
        with self._lock:
            handlers = list(self._handlers.get(msg.topic, []))
        topic   = msg.topic
        payload = msg.payload
        for handler in handlers:
            asyncio.run_coroutine_threadsafe(handler(topic, payload), self._loop)


# ── pipeline session ──────────────────────────────────────────────────────────

class PipelineSession:
    """
    Async orchestration of a single /pray pipeline run.

    Registers MQTT handlers, runs inject.py as a subprocess, waits for
    each stage in order, and returns a PipelineResult.
    """

    def __init__(self, bridge: MQTTBridge, phrase: str):
        self._bridge = bridge
        self._phrase = phrase

        self._transcript_ev   = asyncio.Event()
        self._transcript_text: Optional[str] = None
        self._verse_ev      = asyncio.Event()
        self._speaking_ev   = asyncio.Event()
        self._sentiment_ev  = asyncio.Event()
        self._flare_ev      = asyncio.Event()
        self._done_ev       = asyncio.Event()

        self._inject_start:    Optional[float] = None
        self._inject_end:      Optional[float] = None
        self._transcript_time: Optional[float] = None

        self._verse_text:    Optional[str]  = None
        self._sentiment_raw: Optional[list] = None

        # elapsed seconds from transcript time for each stage
        self._elapsed: dict[str, float] = {}

        self._flare_count    = 0
        self._flare_total_ms = 0
        self._bigjet_count   = 0
        self._bigjet_total_ms = 0

    async def _handle(self, topic: str, payload: bytes):
        now = time.monotonic()
        t0  = self._transcript_time

        if topic == TOPIC_TRANSCRIPT:
            if not self._transcript_ev.is_set():
                self._transcript_time = now
                self._elapsed["stt/transcript"] = now - self._inject_start
                try:
                    self._transcript_text = json.loads(payload).get("text", "").strip()
                except Exception:
                    self._transcript_text = payload.decode(errors="replace").strip()
                self._transcript_ev.set()
            return

        if t0 is None:
            return   # transcript not yet received, ignore downstream messages

        if topic == TOPIC_VERSE and not self._verse_ev.is_set():
            try:
                data = json.loads(payload)
                self._verse_text = data.get("text", "").strip()
            except Exception:
                self._verse_text = payload.decode(errors="replace").strip()
            self._elapsed["t2v/verse"] = now - t0
            self._verse_ev.set()

        elif topic == TOPIC_SPEAKING and not self._speaking_ev.is_set():
            self._elapsed["tts/speaking"] = now - t0
            self._speaking_ev.set()

        elif topic == TOPIC_SENTIMENT and not self._sentiment_ev.is_set():
            try:
                data = json.loads(payload)
                self._sentiment_raw = data.get("classification")
            except Exception:
                pass
            self._elapsed["sentiment/result"] = now - t0
            self._sentiment_ev.set()

        elif topic == TOPIC_FLARE and not self._done_ev.is_set():
            if not self._flare_ev.is_set():
                self._elapsed["flare pulse"] = now - t0
                self._flare_ev.set()
            try:
                self._flare_count    += 1
                self._flare_total_ms += int(payload)
            except (ValueError, TypeError):
                pass

        elif topic == TOPIC_BIGJET and not self._done_ev.is_set():
            try:
                self._bigjet_count    += 1
                self._bigjet_total_ms += int(payload)
            except (ValueError, TypeError):
                pass

        elif topic == TOPIC_DONE and not self._done_ev.is_set():
            self._elapsed["tts/done"] = now - t0
            self._done_ev.set()

    async def run(self, on_verse: Optional[Callable] = None) -> PipelineResult:
        """
        Run the pipeline end-to-end.

        on_verse: async callable(verse_text) called as soon as the verse arrives.
        """
        all_topics = [
            TOPIC_TRANSCRIPT, TOPIC_VERSE, TOPIC_SPEAKING,
            TOPIC_SENTIMENT, TOPIC_FLARE, TOPIC_BIGJET, TOPIC_DONE,
        ]
        for topic in all_topics:
            self._bridge.add_handler(topic, self._handle)

        try:
            self._inject_start = time.monotonic()

            # run bush-pray asynchronously; track when playback finishes
            inject_script = REPO_DIR / "utils" / "bush-pray"
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(inject_script), "--phrase", self._phrase,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )

            async def _record_inject_end():
                await proc.wait()
                self._inject_end = time.monotonic()
            asyncio.ensure_future(_record_inject_end())

            # wait for STT transcript
            try:
                await asyncio.wait_for(self._transcript_ev.wait(), timeout=T_TRANSCRIPT)
            except asyncio.TimeoutError:
                pass

            if not self._transcript_ev.is_set():
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
                return self._build_result(inject_elapsed=time.monotonic() - self._inject_start)

            # wait for verse
            remaining = T_VERSE - self._elapsed.get("stt/transcript", 0)
            try:
                await asyncio.wait_for(self._verse_ev.wait(), timeout=max(5.0, remaining))
            except asyncio.TimeoutError:
                pass

            if self._verse_ev.is_set() and on_verse and self._verse_text:
                try:
                    await on_verse(self._verse_text, self._transcript_text)
                except Exception as e:
                    print(f"[session] on_verse callback error: {e}", flush=True)

            # wait for tts/done (covers speaking, sentiment, flare in parallel)
            transcript_to_now = time.monotonic() - self._transcript_time
            done_timeout = max(10.0, T_DONE - transcript_to_now)
            try:
                await asyncio.wait_for(self._done_ev.wait(), timeout=done_timeout)
            except asyncio.TimeoutError:
                pass

            await proc.wait()
            return self._build_result(inject_elapsed=time.monotonic() - self._inject_start)

        finally:
            for topic in all_topics:
                self._bridge.remove_handler(topic, self._handle)

    def _build_result(self, inject_elapsed: float = 0.0) -> PipelineResult:
        stages = []
        all_passed = True

        # split stt/transcript into audio playback + stt recognition
        if self._inject_end is not None and self._transcript_time is not None:
            stages.append(("audio playback",  "pass", self._inject_end - self._inject_start,    T_TRANSCRIPT))
            stages.append(("stt recognition", "pass", self._transcript_time - self._inject_end, T_TRANSCRIPT))
        elif self._elapsed.get("stt/transcript") is not None:
            # inject_end not recorded (e.g. timeout path) — fall back to total
            stages.append(("stt/transcript", "pass", self._elapsed["stt/transcript"], T_TRANSCRIPT))
        else:
            stages.append(("stt/transcript", "fail", None, T_TRANSCRIPT))
            all_passed = False

        for name, timeout in [
            ("t2v/verse",        T_VERSE),
            ("tts/speaking",     8),
            ("sentiment/result", 10),
            ("flare pulse",      15),
            ("tts/done",         T_DONE),
        ]:
            elapsed = self._elapsed.get(name)
            if elapsed is not None:
                stages.append((name, "pass", elapsed, timeout))
            else:
                stages.append((name, "fail", None, timeout))
                all_passed = False

        return PipelineResult(
            verse=self._verse_text,
            transcript=self._transcript_text,
            sentiment=self._sentiment_raw,
            stages=stages,
            flare_count=self._flare_count,
            flare_total_ms=self._flare_total_ms,
            bigjet_count=self._bigjet_count,
            bigjet_total_ms=self._bigjet_total_ms,
            total_elapsed_s=inject_elapsed,
            passed=all_passed,
        )


# ── embed builder ─────────────────────────────────────────────────────────────

def build_summary_embed(phrase: str, result: PipelineResult) -> discord.Embed:
    # determine color from top emotion
    color = 0x95A5A6   # default grey
    top_emotion = None
    scores: dict[str, float] = {}

    if result.sentiment and isinstance(result.sentiment, list):
        for item in result.sentiment:
            if isinstance(item, dict):
                label = item.get("label", "")
                score = item.get("score", 0.0)
                scores[label] = score
        if scores:
            top_emotion = max(scores, key=scores.get)
            color = EMOTION_COLORS.get(top_emotion, color)

    desc_parts = [f"**said** {phrase}"]
    if result.transcript:
        desc_parts.append(f"**heard** {result.transcript}")
    if result.verse:
        desc_parts.append(f"> *\"{' '.join(result.verse.split())}\"*")
    embed = discord.Embed(
        title=None,
        description="\n".join(desc_parts),
        color=color,
    )

    # stages field
    icons = {"pass": "✅", "fail": "❌", "skip": "⏭️"}
    lines = []
    for name, status, elapsed, timeout in result.stages:
        icon = icons.get(status, "❓")
        if elapsed is not None:
            lines.append(f"{icon} `{name}` — {elapsed:.1f}s")
        else:
            lines.append(f"{icon} `{name}` — timeout >{timeout}s")
    if lines:
        embed.add_field(name="Stages", value="\n".join(lines), inline=False)

    # sentiment bar chart
    if scores and top_emotion:
        bars = []
        for emotion in EMOTIONS_ORDER:
            score = scores.get(emotion, 0.0)
            filled = round(score * BAR_WIDTH)
            bar = "█" * filled + "░" * (BAR_WIDTH - filled)
            marker = " ◀" if emotion == top_emotion else ""
            bars.append(f"`{bar}` {emotion} {score:.0%}{marker}")
        embed.add_field(name="Sentiment", value="\n".join(bars), inline=False)

    # fire pulses — compute duty cycle using tts/done window
    window_ms = next(
        (elapsed * 1000 for name, status, elapsed, _ in result.stages
         if name == "tts/done" and status == "pass" and elapsed),
        None,
    )
    def _duty(on_ms: int) -> str:
        if window_ms and window_ms > 0:
            return f"{on_ms / window_ms:.0%}"
        return "n/a"

    pulse_lines = [
        f"flare:  {result.flare_count}× — {result.flare_total_ms} ms on — {_duty(result.flare_total_ms)} duty",
        f"bigjet: {result.bigjet_count}× — {result.bigjet_total_ms} ms on — {_duty(result.bigjet_total_ms)} duty",
    ]
    embed.add_field(name="Fire Pulses", value="\n".join(pulse_lines), inline=True)

    status_word = "PASSED" if result.passed else "FAILED"
    embed.set_footer(text=f"{status_word} · total {result.total_elapsed_s:.1f}s")

    return embed


# ── discord bot ───────────────────────────────────────────────────────────────

class BushBot(discord.Client):

    def __init__(self, guild_id: Optional[int], bridge: MQTTBridge):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states    = True
        super().__init__(intents=intents)
        self.tree         = app_commands.CommandTree(self)
        self._guild_id    = guild_id
        self._bridge      = bridge
        self._lock        = asyncio.Lock()

        # voice state (Phase 1 + 2)
        self._voice_client:   Optional[discord.VoiceClient] = None
        self._tts_source:     Optional[DiscordTTSSource]    = None
        self._loopback_writer: Optional[LoopbackWriter]     = None

        @self.tree.command(name="pray", description="Run the Bush pipeline with a phrase")
        @app_commands.describe(phrase="The phrase to inject into the pipeline")
        async def pray(interaction: discord.Interaction, phrase: str):
            await self._handle_pray(interaction, phrase)

        @self.tree.command(name="join", description="Join your current voice channel")
        async def join(interaction: discord.Interaction):
            await self._cmd_join(interaction)

        @self.tree.command(name="leave", description="Leave the current voice channel")
        async def leave(interaction: discord.Interaction):
            await self._cmd_leave(interaction)

    async def setup_hook(self):
        # Sync globally so DMs work
        try:
            await self.tree.sync()
            print("[bot] Global slash commands synced", flush=True)
        except Exception as e:
            print(f"[bot] Global sync failed: {e}", flush=True)

        # Also try guild sync for instant availability in the server
        if self._guild_id:
            guild = discord.Object(id=self._guild_id)
            self.tree.copy_global_to(guild=guild)
            try:
                await self.tree.sync(guild=guild)
                print(f"[bot] Guild {self._guild_id} slash commands synced", flush=True)
            except discord.Forbidden:
                print(
                    f"[bot] Warning: missing permissions to sync to guild {self._guild_id}. "
                    "DMs will still work.",
                    flush=True,
                )
            except Exception as e:
                print(f"[bot] Warning: guild sync error: {e}. DMs will still work.", flush=True)

    async def on_ready(self):
        print(f"[bot] Logged in as {self.user} (id={self.user.id})", flush=True)

    async def on_message(self, message: discord.Message):
        print(f"[bot] on_message: author={message.author} bot={message.author.bot} channel={message.channel} content={message.content!r:.60}", flush=True)
        # ignore bots and empty messages
        if message.author.bot:
            return
        phrase = message.content.strip()
        if not phrase:
            return

        # accept DMs and messages in #bush-irl
        is_dm = isinstance(message.channel, discord.DMChannel)
        is_bush_irl = (
            isinstance(message.channel, discord.TextChannel)
            and message.channel.name == "bush-irl"
        )
        if not is_dm and not is_bush_irl:
            return

        if self._lock.locked():
            await message.channel.send(
                "Pipeline is currently running — your request is queued, please wait…"
            )

        async with self._lock:
            print(f"[bot] message '{phrase}' from {message.author} in {message.channel}", flush=True)

            verse_sent = False

            async def on_verse(text: str, heard: Optional[str]):
                nonlocal verse_sent
                try:
                    verse = " ".join(text.split())
                    header = f"{message.author.display_name} said \"{phrase}\" and AI Am (your fucking god dude) heard \"{heard}\"\n" if heard else ""
                    await message.channel.send(f"{header}> *\"{verse}\"*")
                    verse_sent = True
                except Exception as e:
                    print(f"[bot] Failed to send verse: {e}", flush=True)
            # Text stomps on voice: stop current VC playback and mute voice input
            if self._voice_client and self._voice_client.is_playing():
                self._voice_client.stop()
            if self._loopback_writer:
                self._loopback_writer.muted = True

            session = PipelineSession(self._bridge, phrase)
            try:
                result = await session.run(on_verse=on_verse)
            finally:
                if self._loopback_writer:
                    self._loopback_writer.muted = False

            if not verse_sent and result.verse:
                await message.channel.send(f"> *\"{result.verse}\"*")

            embed = build_summary_embed(phrase, result)
            await message.channel.send(embed=embed)

    async def _handle_pray(self, interaction: discord.Interaction, phrase: str):
        phrase = phrase.strip()
        if not phrase:
            await interaction.response.send_message("Please provide a phrase.", ephemeral=True)
            return

        # redirect guild channel messages to #bush-irl
        if isinstance(interaction.channel, discord.TextChannel) and interaction.channel.name != "bush-irl":
            bush_irl = discord.utils.get(interaction.guild.text_channels, name="bush-irl")
            dest = bush_irl.mention if bush_irl else "#bush-irl"
            await interaction.response.send_message(f"I live in {dest} now.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)

        if self._lock.locked():
            await interaction.followup.send(
                "Pipeline is currently running — your request is queued, please wait…",
                ephemeral=True,
            )

        async with self._lock:
            print(f"[bot] /pray '{phrase}' from {interaction.user}", flush=True)

            verse_sent = False

            async def on_verse(text: str, heard: Optional[str]):
                nonlocal verse_sent
                try:
                    verse = " ".join(text.split())
                    header = f"{interaction.user.display_name} said \"{phrase}\" and AI Am (your fucking god dude) heard \"{heard}\"\n" if heard else ""
                    await interaction.followup.send(f"{header}> *\"{verse}\"*")
                    verse_sent = True
                except Exception as e:
                    print(f"[bot] Failed to send verse message: {e}", flush=True)
            # Text stomps on voice: stop current VC playback and mute voice input
            if self._voice_client and self._voice_client.is_playing():
                self._voice_client.stop()
            if self._loopback_writer:
                self._loopback_writer.muted = True

            session = PipelineSession(self._bridge, phrase)
            try:
                result = await session.run(on_verse=on_verse)
            finally:
                if self._loopback_writer:
                    self._loopback_writer.muted = False

            if not verse_sent and result.verse:
                try:
                    await interaction.followup.send(f"> *\"{result.verse}\"*")
                except Exception as e:
                    print(f"[bot] Failed to send late verse: {e}", flush=True)

            embed = build_summary_embed(phrase, result)
            try:
                await interaction.followup.send(embed=embed)
            except Exception as e:
                print(f"[bot] Failed to send summary embed: {e}", flush=True)

    # ── voice commands ────────────────────────────────────────────────────────

    async def _cmd_join(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Voice channels only work in a server.", ephemeral=True)
            return

        member = interaction.guild.get_member(interaction.user.id)
        if not member or not member.voice or not member.voice.channel:
            await interaction.response.send_message("You need to be in a voice channel first.", ephemeral=True)
            return

        channel = member.voice.channel

        # disconnect from existing VC if any
        if self._voice_client and self._voice_client.is_connected():
            await self._leave_voice()

        await interaction.response.defer(thinking=True)
        try:
            await self._join_voice(channel)
            recv_note = " (with voice receive)" if HAS_VOICE_RECV else ""
            await interaction.followup.send(f"Joined **{channel.name}**{recv_note}.")
        except Exception as e:
            print(f"[bot] Failed to join VC: {e}", flush=True)
            await interaction.followup.send(f"Failed to join voice channel: {e}", ephemeral=True)

    async def _cmd_leave(self, interaction: discord.Interaction):
        if not self._voice_client or not self._voice_client.is_connected():
            await interaction.response.send_message("Not currently in a voice channel.", ephemeral=True)
            return
        await self._leave_voice()
        await interaction.response.send_message("Left voice channel.")

    async def _join_voice(self, channel: discord.VoiceChannel):
        """Connect to a voice channel and start the TTS source (+ optional receive)."""
        # Phase 2: use VoiceRecvClient if available
        if HAS_VOICE_RECV:
            vc = await channel.connect(cls=voice_recv.VoiceRecvClient)
        else:
            vc = await channel.connect()

        self._voice_client = vc
        self._tts_source   = DiscordTTSSource()
        print(f"[bot] Joined VC {channel.name}, TTS source ready", flush=True)

        # Global verse handler: synthesize to VC for ALL pipeline runs (text or voice triggered)
        self._bridge.add_handler(TOPIC_VERSE, self._on_verse_global)

        # Phase 2: set up loopback writer + voice receive sink
        if HAS_VOICE_RECV and isinstance(vc, voice_recv.VoiceRecvClient):
            self._loopback_writer = LoopbackWriter()
            self._loopback_writer.start()

            loopback    = self._loopback_writer
            opus_dec    = discord.opus.Decoder()
            import davey as _davey


            def on_audio(user, data: voice_recv.VoiceData):
                raw = data.opus  # RTP-decrypted, DAVE-encrypted Opus bytes
                if not raw:
                    return
                # DAVE layer: look up session dynamically (it may re-key after join)
                dave = vc._connection.dave_session
                if dave and user:
                    try:
                        raw = dave.decrypt(user.id, _davey.MediaType.audio, raw)
                    except Exception as e:
                        # UnencryptedWhenPassthroughDisabled = transitional packet, skip silently
                        if "UnencryptedWhenPassthroughDisabled" not in str(e):
                            print(f"[voice-recv] DAVE decrypt error: {e}", flush=True)
                        return
                elif dave and not user:
                    return  # user unknown, can't decrypt
                # Opus → PCM
                try:
                    pcm = opus_dec.decode(raw, fec=False)
                except Exception as e:
                    print(f"[voice-recv] opus decode error: {e}", flush=True)
                    return
                loopback.push(pcm)

            def on_listen_end(error):
                if error:
                    print(f"[voice-recv] reader stopped with error: {error}", flush=True)

            vc.listen(voice_recv.BasicSink(on_audio, decode=False), after=on_listen_end)
            print(f"[bot] Voice receive active → loopback", flush=True)

            # echo mute gate: mute loopback while TTS is speaking
            self._bridge.add_handler(TOPIC_SPEAKING, self._on_speaking_mute)
            self._bridge.add_handler(TOPIC_DONE,     self._on_done_unmute)

    async def _leave_voice(self):
        """Disconnect from VC and clean up all voice resources."""
        vc = self._voice_client
        if vc:
            if vc.is_playing():
                vc.stop()
            if vc.is_connected():
                await vc.disconnect()
        self._voice_client = None
        self._tts_source   = None

        self._bridge.remove_handler(TOPIC_VERSE, self._on_verse_global)

        if self._loopback_writer:
            self._loopback_writer.stop()
            self._loopback_writer = None
            self._bridge.remove_handler(TOPIC_SPEAKING, self._on_speaking_mute)
            self._bridge.remove_handler(TOPIC_DONE,     self._on_done_unmute)

        print("[bot] Left VC, voice resources cleaned up", flush=True)

    def _on_vc_play_end(self, error: Optional[Exception]):
        if error:
            print(f"[bot] VC play error: {error}", flush=True)

    async def _on_verse_global(self, topic: str, payload: bytes):
        """Global TOPIC_VERSE handler: synthesize verse to VC whenever bot is joined."""
        vc  = self._voice_client
        src = self._tts_source
        if not vc or not src or not vc.is_connected():
            return
        try:
            data = json.loads(payload)
            text = data.get("text", "").strip()
            if not text:
                return
            first_para = text.split("\n\n")[0]
            clean = " ".join(line.strip() for line in first_para.splitlines() if line.strip())
            pcm = await src.synthesize(clean)
            if not pcm:
                return
            # Stop any in-progress playback, load new audio, start playing
            if vc.is_playing():
                vc.stop()
            src.load(pcm)
            vc.play(src, after=self._on_vc_play_end)
        except Exception as e:
            print(f"[bot] verse-to-vc error: {e}", flush=True)

    async def _on_speaking_mute(self, topic: str, payload: bytes):
        if self._loopback_writer:
            self._loopback_writer.muted = True

    async def _on_done_unmute(self, topic: str, payload: bytes):
        if self._loopback_writer:
            self._loopback_writer.muted = False


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        print("Error: DISCORD_TOKEN environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    guild_id_str = os.environ.get("DISCORD_GUILD_ID")
    guild_id     = int(guild_id_str) if guild_id_str else None

    broker_override = os.environ.get("MQTT_BROKER")
    if broker_override:
        broker = broker_override
    else:
        sys.path.insert(0, str(REPO_DIR))
        from bushutil import get_mqtt_broker
        broker = get_mqtt_broker()

    print(f"[bot] MQTT broker: {broker}", flush=True)
    if HAS_VOICE_RECV:
        print("[bot] discord-ext-voice-recv available — Phase 2 voice receive enabled", flush=True)
    else:
        print("[bot] discord-ext-voice-recv not installed — voice output only (Phase 1)", flush=True)

    loop   = asyncio.new_event_loop()
    bridge = MQTTBridge(broker, loop)

    print(f"[bot] Connecting to MQTT …", flush=True)
    if not bridge.connect():
        print("Error: could not connect to MQTT broker.", file=sys.stderr)
        sys.exit(1)
    print(f"[bot] MQTT connected.", flush=True)

    bot = BushBot(guild_id=guild_id, bridge=bridge)

    async def _run():
        try:
            await bot.start(token)
        finally:
            if bot._voice_client:
                await bot._leave_voice()
            bridge.disconnect()

    try:
        loop.run_until_complete(_run())
    except KeyboardInterrupt:
        loop.run_until_complete(bot.close())


if __name__ == "__main__":
    main()
