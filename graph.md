Yeh wahi flow hai, lekin har scenario ek example ke saath:

```mermaid
flowchart TD
    A["Request: start=Chicago, IL · finish=Denver, CO"] --> B["urls.py: api/route/ match"]
    B --> C{"views.py: input theek hai?"}
    C -- "start khali / dono same" --> E1["400 error<br/>'Both start and finish are required'"]
    C -- Haan --> D{"Location US mein hai?"}
    D -- "Toronto, ON" --> E2["400 error<br/>'Only locations inside the USA'"]
    D -- "Chicago, IL ✔" --> F{"Yeh trip pehle cache mein hai?"}
    F -- "Haan (2nd request)" --> R2["Foran jawab<br/>external_api_calls: 0, cached: true"]
    F -- "Nahi (1st request)" --> G{"OSRM call chali?"}
    G -- "OSRM down / timeout" --> E3["502 error<br/>'Routing service unavailable'"]
    G -- "Haan, 1 call" --> H["KD-tree: road se 10 miles ke andar stations"]
    H --> I{"Har 500 miles mein station mila?"}
    I -- Nahi --> E4["422 error<br/>'No fuel station within 500 miles'"]
    I -- Haan --> J["Greedy plan: sasta aage? utna lo, warna full"]
    J --> R1["200 OK JSON<br/>external_api_calls: 1, cached: false"]
    R1 --> M["map.html: route line + stop markers"]
    R2 --> M
```

Is graph ko Loom mein aise bol sakte hain:

- Normal case mein Chicago se Denver ki pehli request par sirf 1 OSRM call hoti hai.
- Wahi trip doosri dafa bhejein to answer cache se aata hai aur API calls 0 hoti hain.
- Galat input ya Canada ka shehar ho to 400 aata hai, OSRM band ho to 502, aur 500 miles mein koi station na mile to 422.
