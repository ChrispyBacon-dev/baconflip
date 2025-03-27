import redis.asyncio as redis
import os
import json
import logging

# Configure logging
logger = logging.getLogger(__name__)

# --- Global Variables ---
redis_pool = None
MAX_HISTORY_MESSAGES = 0 # Max individual messages (user + bot)

# --- Initialization ---
def initialize_redis_pool():
    """Initializes the Redis connection pool using environment variables."""
    global redis_pool, MAX_HISTORY_MESSAGES
    if redis_pool is not None:
        logger.debug("Redis pool already initialized.")
        return

    try:
        redis_host = os.getenv('REDIS_HOST', 'localhost')
        redis_port = int(os.getenv('REDIS_PORT', 6379))
        redis_password = os.getenv('REDIS_PASSWORD') # Can be None
        history_turns = int(os.getenv('HISTORY_LENGTH', 10))
        # Calculate max messages: N turns = N user msgs + N bot msgs = 2N total
        MAX_HISTORY_MESSAGES = max(2, history_turns * 2) # Ensure at least 2

        redis_pool = redis.ConnectionPool(
            host=redis_host,
            port=redis_port,
            password=redis_password,
            decode_responses=True, # Decode responses from bytes to strings automatically
            socket_timeout=10,     # Timeout for socket operations
            socket_connect_timeout=10, # Timeout for initial connection
            health_check_interval=30 # Optional: Check connection periodically
        )
        logger.info(f"Redis pool initialized for {redis_host}:{redis_port}. Max history length: {MAX_HISTORY_MESSAGES} messages ({history_turns} turns).")
    except Exception as e:
        logger.error(f"Failed to initialize Redis pool: {e}", exc_info=True)
        redis_pool = None # Ensure pool is None on failure
        raise ConnectionError("Could not initialize Redis connection pool") from e

async def get_redis_client():
    """Gets a Redis client from the pool. Ensures pool is initialized."""
    if redis_pool is None:
        try:
             initialize_redis_pool() # Attempt initialization if not done yet
        except ConnectionError as conn_err:
             logger.error(f"Failed to get Redis client due to pool init failure: {conn_err}")
             raise ConnectionError("Redis pool is not available.") from conn_err

    # If initialization succeeded or was already done
    return redis.Redis(connection_pool=redis_pool)

# Optional: Add function to close pool if needed during shutdown
# async def close_redis_pool():
#     global redis_pool
#     if redis_pool:
#         logger.info("Closing Redis connection pool...")
#         try:
#             await redis_pool.disconnect(inuse_connections=True) # Wait for connections to finish
#         except Exception as e:
#             logger.error(f"Error disconnecting Redis pool: {e}")
#         finally:
#             redis_pool = None


# --- History Functions ---
async def get_history(channel_id: int, user_id: int) -> list:
    """Retrieves conversation history from Redis as a list of message dicts."""
    key = f"history:{channel_id}:{user_id}"
    try:
        redis_client = await get_redis_client()
        # LRANGE gets elements from start (0) to end (-1)
        history_json_list = await redis_client.lrange(key, 0, -1)
        history = [json.loads(msg_json) for msg_json in history_json_list]
        logger.debug(f"Retrieved history for {key}: {len(history)} messages")
        return history
    except json.JSONDecodeError as e:
        logger.error(f"Error decoding JSON from Redis history for key {key}: {e}. Data might be corrupted.", exc_info=True)
        # Optionally try to delete the bad key here
        # try: await redis_client.delete(key); logger.warning(f"Deleted corrupted history key {key}") except: pass
        return []
    except redis.RedisError as e:
        logger.error(f"Redis error getting history for key {key}: {e}", exc_info=True)
        return [] # Return empty list on Redis error
    except ConnectionError as e: # Catch if get_redis_client failed
        logger.error(f"Failed to get Redis client for history lookup: {e}")
        return []
    except Exception as e:
        logger.error(f"Unexpected error getting history for key {key}: {e}", exc_info=True)
        return []

async def add_to_history(channel_id: int, user_id: int, user_message_content: str, bot_message_content: str):
    """Adds user and bot messages to history in Redis, trims old messages."""
    if not user_message_content or not bot_message_content:
        logger.warning("Attempted to add empty message to history. Skipping.")
        return

    key = f"history:{channel_id}:{user_id}"
    try:
        redis_client = await get_redis_client()
        # Prepare messages in the standard {role: ..., content: ...} format
        user_msg_obj = {"role": "user", "content": user_message_content}
        bot_msg_obj = {"role": "assistant", "content": bot_message_content}

        # Convert to JSON strings for storage in Redis list
        user_msg_json = json.dumps(user_msg_obj)
        bot_msg_json = json.dumps(bot_msg_obj)

        # Use pipeline for atomic RPUSH and LTRIM
        async with redis_client.pipeline(transaction=True) as pipe:
            # RPUSH adds element(s) to the tail (right end) of the list
            pipe.rpush(key, user_msg_json, bot_msg_json)
            # LTRIM trims the list so that it will contain only elements from
            # index -MAX_HISTORY_MESSAGES (counting from the end) to -1 (the very end).
            pipe.ltrim(key, -MAX_HISTORY_MESSAGES, -1)
            # Execute the pipeline
            results = await pipe.execute()
            # results[0] is result of rpush (new length), results[1] is result of ltrim ('OK')
            logger.debug(f"Added to history for {key}. New length: {results[0]}, Trim OK: {results[1]=='OK'}")

    except json.JSONDecodeError as e:
         logger.error(f"Error encoding message to JSON for key {key}: {e}")
    except redis.RedisError as e:
        logger.error(f"Redis error adding to history for key {key}: {e}", exc_info=True)
    except ConnectionError as e:
         logger.error(f"Failed to get Redis client for adding history: {e}")
    except Exception as e:
        logger.error(f"Unexpected error adding to history for key {key}: {e}", exc_info=True)

async def clear_history(channel_id: int, user_id: int) -> bool:
    """Clears the conversation history for a specific user/channel."""
    key = f"history:{channel_id}:{user_id}"
    try:
        redis_client = await get_redis_client()
        # DELETE returns the number of keys deleted (0 or 1 in this case)
        deleted_count = await redis_client.delete(key)
        if deleted_count > 0:
            logger.info(f"Cleared history for {key}.")
        else:
            logger.info(f"Attempted to clear history for {key}, but key did not exist.")
        return deleted_count > 0
    except redis.RedisError as e:
        logger.error(f"Redis error clearing history for key {key}: {e}", exc_info=True)
        return False
    except ConnectionError as e:
         logger.error(f"Failed to get Redis client for clearing history: {e}")
         return False
    except Exception as e:
        logger.error(f"Unexpected error clearing history for key {key}: {e}", exc_info=True)
        return False

# --- Mute Functions ---

async def set_channel_mute(channel_id: int, muted: bool) -> bool:
    """Sets or clears the mute status for a channel in Redis."""
    key = f"mute:{channel_id}"
    try:
        redis_client = await get_redis_client()
        if muted:
            # Set key with a value of "1". No expiry set here, permanent until unmuted.
            # Example with 1 day expiry: await redis_client.set(key, "1", ex=86400)
            result = await redis_client.set(key, "1")
            if result:
                logger.info(f"Channel {channel_id} muted in Redis.")
                return True
            else:
                logger.error(f"Redis SET command failed for mute key {key}")
                return False
        else:
            # Delete the key to unmute
            deleted_count = await redis_client.delete(key)
            if deleted_count >= 0: # Delete returns number of keys deleted, 0 if not found is ok
                 logger.info(f"Channel {channel_id} unmuted in Redis (or was not muted). Keys deleted: {deleted_count}")
                 return True
            else:
                 # This part should ideally not be reached for DELETE command
                 logger.error(f"Redis DELETE command failed unexpectedly for mute key {key}")
                 return False
    except redis.RedisError as e:
        logger.error(f"Redis error setting mute status for channel {channel_id}: {e}", exc_info=True)
        return False
    except ConnectionError as e:
         logger.error(f"Failed to get Redis client for setting mute status: {e}")
         return False
    except Exception as e:
         logger.error(f"Unexpected error setting mute status for channel {channel_id}: {e}", exc_info=True)
         return False

async def is_channel_muted(channel_id: int) -> bool:
    """Checks if a channel is marked as muted in Redis."""
    key = f"mute:{channel_id}"
    try:
        redis_client = await get_redis_client()
        # EXISTS returns 1 if the key exists, 0 otherwise.
        result = await redis_client.exists(key)
        is_muted_status = result > 0
        logger.debug(f"Checked mute status for channel {channel_id}: {'Muted' if is_muted_status else 'Not Muted'}")
        return is_muted_status
    except redis.RedisError as e:
        logger.error(f"Redis error checking mute status for channel {channel_id}: {e}", exc_info=True)
        return False # Fail safe: assume not muted on error
    except ConnectionError as e:
         logger.error(f"Failed to get Redis client for checking mute status: {e}")
         return False # Fail safe
    except Exception as e:
        logger.error(f"Unexpected error checking mute status for channel {channel_id}: {e}", exc_info=True)
        return False # Fail safe
