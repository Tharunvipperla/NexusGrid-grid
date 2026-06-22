# Live Topology

**Sidebar → My node → Live Topology.** A live graph of the nodes you can see and
how they relate — who hosts what for whom, where compute flows, and the health of
each link.

---

## The graph

Each node is drawn as a vertex; edges show active relationships. Click a node to
inspect the relationship between you and it, including:

| Metric | Meaning |
|---|---|
| **Compute received / given** | Work you've consumed from them vs served to them. |
| **They host for you / You host for them** | Foreign-storage deposits in each direction. |
| **Task success** · **Tasks ok / failed** | Reliability of work between you. |
| **Live flows** | Transfers/streams happening right now. |
| **Services you hold** · **Tasks on their node** · **Deposits you store there** | What you use of theirs. |
| **Their tasks on you** · **Their deposits on you** | What they use of yours. |

A **Legend** explains the colours/line styles, and a **Recent** panel lists the
latest topology changes.

---

## Performance (large grids)

Rendering hundreds of nodes can be heavy. In **[Interface settings](../interface.md#interface-settings)**
you can cap **"Topology — nodes to draw"**; the graph always draws the
most-relevant N, and **search/filters reach the rest** at any size. If you ask for
a count that would stutter, it warns you and lets you apply anyway.
