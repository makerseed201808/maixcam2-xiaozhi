# SIoT MCP completion plan

**Goal:** Complete the approved topic, history, saved publish, subscription, topic management, media and device-feedback interfaces; leave device testing for the user's later session.

**Architecture:** Extend the existing independent MQTT transport and bounded REST adapter. Keep foreground media operations in the monitor process, using authenticated-local process ownership rather than competing display/camera owners. Correlate device replies by command ID and never equate a broker acknowledgement with actuator execution. All network operations run off the voice event loop.

**Tech Stack:** Existing Python, Paho MQTT 2, urllib, MaixPy/Pillow media components, unittest. No new dependencies.

## Implementation and verification

- [x] Catalog/history/mutations: verify supplied API schema offline, support bounded pagination; latest messages label history vs live. Create is idempotent; destructive actions require expiring single-use confirmation bound to full topic, action and ID.
- [x] MQTT: saved publish registers topic then QoS1 with honest acknowledgement/persistence semantics; explicit subscribe/unsubscribe/list; cancellation prevents pending writes, no implicit replay.
- [x] Media: local IPC to existing monitor for start/stop/show/status; preserve UI and camera ownership, bounded requests, confirmation/error/status; install complete files. No in-service display initialization.
- [x] Device commands: local configured devices, exact command/reply topics, unique IDs, subscribe-before-publish, validate matched device/command reply, no retry, timeout is unknown execution.
- [x] MCP integration: register schemas, redact payloads, thread dispatch, scoped destructive confirmation, lifecycle cleanup, deployment source completeness.
- [x] Host suite and independent review: focused regressions plus full tests and whitespace/compile checks. Include local HTTP/socket/MQTT fakes rather than pretend hardware.
- [x] Docs: tool table, voice examples, connection and ESP32 protocol examples, staged hardware checklist, clear implemented/deployed distinction.

## Deferred by user

No live device testing or deployment this round. Real voice, installed-server API compatibility, touch/display, media performance, and ESP32 physical execution remain explicit later acceptance gates. Earlier topic-list device failure must not be labelled repaired on-device without that later test.
