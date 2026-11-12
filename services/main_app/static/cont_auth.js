// Continuous Authentication Telemetry Collector
// Passively collects user interactions and sends them via WebSockets

(function () {
    let eventBuffer = [];
    let sequenceId = 0;
    const BATCH_INTERVAL_MS = 3000;
    let ws = null;
    let flushInterval = null;

    function getCookie(name) {
        const value = `; ${document.cookie}`;
        const parts = value.split(`; ${name}=`);
        if (parts.length === 2) return parts.pop().split(';').shift();
        return null;
    }

    const sessionId = getCookie('session_id');
    if (!sessionId) {
        console.warn("No session_id cookie found, cont_auth.js will not run.");
        return;
    }

    // Initialize WebSocket
    function connectWebSocket() {
        ws = new WebSocket('ws://localhost:8001/ws/telemetry/' + sessionId);

        ws.onopen = () => {
            console.log('Connected to Continuous Auth WebSocket');
            flushInterval = setInterval(flushBuffer, BATCH_INTERVAL_MS);
        };

        ws.onclose = (event) => {
            console.log('Continuous Auth WebSocket closed', event.reason);
            clearInterval(flushInterval);
            // If the server revoked the session, we should probably redirect
            if (event.code === 1008) { // Policy Violation / Trust score too low
                window.location.href = '/login'; // Or rely on the next user action
            }
        };

        ws.onerror = (error) => {
            console.error('WebSocket Error:', error);
        };
    }

    function addEvent(type, extraData) {
        const ts = Date.now();
        const ev = { t: type, ts: ts, ...extraData };
        eventBuffer.push(ev);
    }

    // --- Event Listeners ---
    let lastMouseTs = 0;
    document.addEventListener('mousemove', (e) => {
        const now = Date.now();
        if (now - lastMouseTs > 50) {
            addEvent('mm', { x: e.clientX, y: e.clientY });
            lastMouseTs = now;
        }
    });

    document.addEventListener('mousedown', (e) => {
        addEvent('md', { x: e.clientX, y: e.clientY, btn: e.button });
    });

    document.addEventListener('mouseup', (e) => {
        addEvent('mu', { x: e.clientX, y: e.clientY, btn: e.button });
    });

    document.addEventListener('keydown', (e) => {
        const isAlphanumeric = /^[a-zA-Z0-9]$/.test(e.key);
        const keyVal = isAlphanumeric ? 'char' : e.key;
        addEvent('kd', { key: keyVal });
    });

    document.addEventListener('keyup', (e) => {
        const isAlphanumeric = /^[a-zA-Z0-9]$/.test(e.key);
        const keyVal = isAlphanumeric ? 'char' : e.key;
        addEvent('ku', { key: keyVal });
    });


    // --- Batch Sending ---
    function flushBuffer() {
        if (!ws || ws.readyState !== WebSocket.OPEN || eventBuffer.length === 0) {
            return;
        }

        const payload = {
            sequence_id: sequenceId++,
            batch_timestamp: Date.now(),
            events: [...eventBuffer]
        };

        eventBuffer = [];
        ws.send(JSON.stringify(payload));
    }

    // Connect
    connectWebSocket();

})();
