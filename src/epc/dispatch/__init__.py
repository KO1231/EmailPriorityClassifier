"""Where mutations go, and how they come back.

Classification decides *what should change*; nothing here decides anything. A
sink takes a mutation and is responsible only for getting it somewhere — applied
now, written to a file, or put on a queue for another process.

The split exists so the two halves can fail, scale and deploy independently:
today a Gmail 429 during the write phase costs the classification work that
produced it. It also makes dry-run a sink rather than a flag checked inside a
loop, which means the thing you reviewed is exactly the thing you can replay.
"""
