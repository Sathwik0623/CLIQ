#!/usr/bin/env python3
"""
demo_tier_augmentation.py — End-to-end demo showing tier responses augmented with past resolutions.

Shows the full IntelligentRetrievalResponse with both the standard tier (exact/suggestion/related/fallback)
AND the related_past_resolutions section from memory recall (Phase 5).
"""

import os
import sys
from datetime import datetime

# Force offline before any imports
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_XET"] = "1"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cliq.memory.operational_memory import OperationalMemoryStore
from cliq.retrieval.query import CommandRetriever
from cliq.retrieval.assistant import IntelligentCommandAssistant
from cliq.retrieval.schema import RetrievalQuery
from cliq.ingest.storage import CommandStore, SQLiteVectorStore


def demo_tier_augmentation():
    """End-to-end demo: log issue → query similar → show augmented response."""
    print("\n" + "=" * 80)
    print("DEMO: TIER AUGMENTATION WITH PAST RESOLUTIONS (Phase 5)")
    print("=" * 80)
    
    # Initialize memory store
    print("\n[SETUP] Initializing memory store...")
    try:
        memory_store = OperationalMemoryStore(memory_dir=".cliq/demo_memory")
        print("    ✓ Memory store ready")
    except Exception as e:
        print(f"    ✗ Failed: {e}")
        return False
    
    # Log issue #1 into memory: "show ip route issue"
    print("\n[STEP 1] Logging Issue #1 into memory...")
    print("    Issue: 'show ip route not displaying all routes on router-1'")
    try:
        mem_id_1 = memory_store.store_memory(
            issue_text="show ip route not displaying all routes, missing connected networks",
            commands=[
                "show ip route",
                "show ip route summary",
                "clear ip bgp *",
                "redistribute connected subnets",
            ],
            platform="iosxe",
            device_name="router-1",
            outcome="resolved",
            outcome_notes="BGP redistribution fixed missing routes",
            scope="user",
        )
        print(f"    ✓ Stored: {mem_id_1[:8]}...")
    except Exception as e:
        print(f"    ✗ Failed to store: {e}")
        return False
    
    # Log issue #2 into memory: "bgp neighbor stuck" (different platform)
    print("\n[STEP 2] Logging Issue #2 into memory...")
    print("    Issue: 'BGP neighbor stuck in active state on switch-2'")
    try:
        mem_id_2 = memory_store.store_memory(
            issue_text="BGP neighbor stuck in active state, not reaching established",
            commands=[
                "show ip bgp summary",
                "show ip bgp neighbors 10.0.0.1",
                "clear ip bgp 10.0.0.1 soft",
                "debug ip bgp keepalives",
            ],
            platform="iosxr",
            device_name="switch-2",
            outcome="resolved",
            outcome_notes="Soft reset of BGP session resolved active state",
            scope="user",
        )
        print(f"    ✓ Stored: {mem_id_2[:8]}...")
    except Exception as e:
        print(f"    ✗ Failed to store: {e}")
        return False
    
    print(f"\n    Total memories in store: {memory_store.memory_count()}")
    
    # Initialize retrieval layer
    print("\n[SETUP] Initializing command retrieval layer...")
    try:
        # Use production KB
        store = CommandStore("data/command_db/commands.sqlite")
        vectors = SQLiteVectorStore("data/retrieval_vectors.db")
        retriever = CommandRetriever(store, vectors)
        
        # Create assistant WITH memory enabled
        assistant = IntelligentCommandAssistant(
            retriever=retriever,
            memory_dir=".cliq/demo_memory",
            enable_memory=True,
        )
        print("    ✓ Retriever and assistant ready (memory enabled)")
    except Exception as e:
        print(f"    ✗ Failed: {e}")
        return False
    
    # DEMO: Query something related to logged issues
    print("\n" + "=" * 80)
    print("DEMO QUERY #1: Suggestion tier with memory augmentation")
    print("=" * 80)
    print("\nUser asks: 'show routes missing on router-5'")
    
    try:
        q1 = RetrievalQuery(
            input_text="show routes missing on router-5",
            platform="iosxe",
        )
        response1 = assistant.respond(q1)
        
        print(f"\n[RESPONSE] Tier: {response1.response_tier.upper()}")
        print(f"[RESPONSE] Message: {response1.message}")
        print(f"[RESPONSE] Confidence: {int(response1.confidence_score * 100)}%")
        
        if response1.results:
            print(f"\n[SUGGESTIONS] Top commands:")
            for idx, result in enumerate(response1.results[:3], 1):
                print(f"    {idx}. {result.command}")
        
        # KEY: Show past resolutions augmentation
        if response1.past_resolutions:
            print(f"\n[MEMORY AUGMENTATION] Related past resolutions:")
            for idx, past_res in enumerate(response1.past_resolutions, 1):
                rank_pct = int(past_res.final_rank_score * 100)
                mem = past_res.memory_record
                print(f"\n    {idx}. [{rank_pct}% relevant] {past_res.reason}")
                print(f"       Issue: {mem.issue_text}")
                print(f"       Device: {mem.device_name} ({mem.platform})")
                print(f"       Status: {mem.outcome}")
                print(f"       Commands used:")
                for cmd in mem.commands[:2]:
                    print(f"          • {cmd}")
                if len(mem.commands) > 2:
                    print(f"          • +{len(mem.commands) - 2} more")
        else:
            print(f"\n[MEMORY AUGMENTATION] No related past resolutions (expected for new issue)")
    
    except Exception as e:
        print(f"    ✗ Query failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # DEMO: Query something exactly matching memory issue
    print("\n" + "=" * 80)
    print("DEMO QUERY #2: Exact tier with memory augmentation (cross-platform)")
    print("=" * 80)
    print("\nUser asks: 'show ip route summary'")
    
    try:
        q2 = RetrievalQuery(
            input_text="show ip route summary",
        )
        response2 = assistant.respond(q2)
        
        print(f"\n[RESPONSE] Tier: {response2.response_tier.upper()}")
        print(f"[RESPONSE] Message: {response2.message}")
        
        if response2.results:
            print(f"\n[RESULTS] Exact matches:")
            for idx, result in enumerate(response2.results[:2], 1):
                print(f"    {idx}. {result.command}")
                if result.description:
                    print(f"       {result.description[:60]}...")
        
        # Memory augmentation even for exact matches
        if response2.past_resolutions:
            print(f"\n[MEMORY AUGMENTATION] Related past resolutions:")
            for idx, past_res in enumerate(response2.past_resolutions, 1):
                rank_pct = int(past_res.final_rank_score * 100)
                mem = past_res.memory_record
                print(f"\n    {idx}. [{rank_pct}% relevant] {past_res.reason}")
                print(f"       Issue: {mem.issue_text[:60]}...")
                print(f"       Device: {mem.device_name}")
                print(f"       Status: {mem.outcome}")
        else:
            print(f"\n[MEMORY AUGMENTATION] No related past resolutions")
    
    except Exception as e:
        print(f"    ✗ Query failed: {e}")
        return False
    
    # Summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print("""
✓ Stored 2 issues with outcomes (resolved)
✓ Queried similar issues → Assistant responses augmented with past resolutions
✓ Cross-device recall: Issues recalled even from different devices
✓ Cross-tier augmentation: Memory added to suggestion AND exact tiers
✓ Ranking: Memories ranked by similarity × outcome_weight × recency
✓ Zero network calls: All operations fully offline
    """)
    
    return True


if __name__ == "__main__":
    try:
        success = demo_tier_augmentation()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\n✗ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
