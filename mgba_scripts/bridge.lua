-- bridge.lua
--
-- mGBA Lua bridge for Jellyfish Plays Pokemon.
-- Connects to the Python TCP command server, receives button commands each
-- frame, and applies them as a held-then-released key press (a single-frame
-- tap is often not registered reliably by GBA games).
--
-- Load via mGBA: Tools -> Scripting... -> load this file.
--
-- Reference: https://mgba.io/docs/scripting.html

local HOST = "127.0.0.1"
local PORT = 5555
local HOLD_FRAMES = 6          -- how many emulated frames to hold a button
local RECONNECT_INTERVAL_FRAMES = 120  -- retry connect roughly every ~2s at 60fps

local KEY_MAP = {
	A = C.GBA_KEY.A,
	B = C.GBA_KEY.B,
	SELECT = C.GBA_KEY.SELECT,
	START = C.GBA_KEY.START,
	RIGHT = C.GBA_KEY.RIGHT,
	LEFT = C.GBA_KEY.LEFT,
	UP = C.GBA_KEY.UP,
	DOWN = C.GBA_KEY.DOWN,
	R = C.GBA_KEY.R,
	L = C.GBA_KEY.L,
}

local sock = nil
local connecting = false
local recvBuffer = ""
local framesSinceReconnectAttempt = 0

-- Active hold: { key = <GBA_KEY constant>, framesLeft = N }
local activeHold = nil

local function log(msg)
	console:log("[bridge] " .. msg)
end

local function closeSocket()
	if sock then
		pcall(function() sock:close() end)
		sock = nil
	end
end

local function tryConnect()
	if sock then return end
	connecting = true
	log("attempting connection to " .. HOST .. ":" .. PORT .. " ...")
	-- socket.connect() blocks and, per mGBA's scripting API, raises a Lua
	-- error (rather than returning nil) when the connection fails (e.g.
	-- ECONNREFUSED because the Python server isn't up yet). pcall is
	-- required here or a failed attempt silently aborts this whole script.
	local ok, newSock = pcall(socket.connect, HOST, PORT)
	connecting = false
	if not ok then
		log("connection attempt failed: " .. tostring(newSock) .. " (will retry)")
		return
	end
	if not newSock then
		log("connection attempt failed: connect() returned no socket (will retry)")
		return
	end
	newSock:add("received", function()
		-- handled in poll loop via hasdata/receive
	end)
	newSock:add("error", function(err)
		log("socket error: " .. tostring(err))
		closeSocket()
	end)
	sock = newSock
	connecting = false
	recvBuffer = ""
	log("connected to " .. HOST .. ":" .. PORT)
end

local function applyKey(keyConst)
	-- Release any currently-held key before starting a new one so inputs
	-- don't stack.
	if activeHold then
		emu:clearKey(activeHold.key)
	end
	emu:addKey(keyConst)
	activeHold = { key = keyConst, framesLeft = HOLD_FRAMES }
end

local function handleCommand(cmd)
	cmd = cmd:gsub("%s+$", ""):gsub("^%s+", "")
	if cmd == "" then return end
	local keyConst = KEY_MAP[cmd]
	if keyConst == nil then
		log("unknown command: " .. cmd)
		return
	end
	log("received: " .. cmd)
	applyKey(keyConst)
end

local function pollSocket()
	if not sock then return end

	local ok, hasData = pcall(function() return sock:hasdata() end)
	if not ok then
		log("socket read check failed, disconnecting")
		closeSocket()
		return
	end

	if hasData then
		local data, err = sock:receive(4096)
		if data then
			recvBuffer = recvBuffer .. data
			while true do
				local newlineIdx = recvBuffer:find("\n")
				if not newlineIdx then break end
				local line = recvBuffer:sub(1, newlineIdx - 1)
				recvBuffer = recvBuffer:sub(newlineIdx + 1)
				handleCommand(line)
			end
		elseif err and err ~= socket.ERRORS.AGAIN then
			log("socket receive error: " .. tostring(err))
			closeSocket()
		end
	end
end

local function onFrame()
	framesSinceReconnectAttempt = framesSinceReconnectAttempt + 1

	if not sock and not connecting then
		if framesSinceReconnectAttempt >= RECONNECT_INTERVAL_FRAMES then
			framesSinceReconnectAttempt = 0
			tryConnect()
		end
	else
		pollSocket()
	end

	if activeHold then
		activeHold.framesLeft = activeHold.framesLeft - 1
		if activeHold.framesLeft <= 0 then
			emu:clearKey(activeHold.key)
			activeHold = nil
		end
	end
end

callbacks:add("frame", onFrame)

log("bridge.lua loaded, will connect to " .. HOST .. ":" .. PORT)
tryConnect()
