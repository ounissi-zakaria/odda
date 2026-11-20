# odda

Browser automation and HTTP traffic capture for agents; one context: the captured
exchange and the state stored around it.

## Language

**Flow**:
One captured network exchange — a request, its response (or error), and the stored
artifacts for both.
_Avoid_: transaction, hit, pair

**Object**:
The one stored copy of byte-identical captured content. Flows reference objects
rather than storing their own copies; identical content in many flows is one object.
_Avoid_: blob, saved body, content-addressed file
