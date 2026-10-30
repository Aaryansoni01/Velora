const socket = new WebSocket("ws://localhost:8765");

socket.onopen = () => {
    console.log("✅ Connected to Python Backend");
};

socket.onmessage = (event) => {
    console.log("📩 Python:", event.data);
};

socket.onerror = (error) => {
    console.error("WebSocket Error:", error);
};

socket.onclose = () => {
    console.log("❌ Connection Closed");
};

// Test message after connection
socket.onopen = () => {
    console.log("Connected");
    socket.send("Hello Velora");
};