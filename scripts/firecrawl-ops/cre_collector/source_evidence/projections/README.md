Part of [AGENTIC-2895](https://linear.app/agenticassets/issue/AGENTIC-2895).

### Leibsohn page-observation projection

For a private `mode: observe` Leibsohn collector part, the explicit
`leibsohn-page-observation-withholding/v1` version composes the existing
Google Maps tile query projection with whole-value withholding at the observed
nested `input.context.nativeContext.pdf_url` and historical replay
`input.context.originalContext.nativeContext.pdf_url` pointers. Original
null and missing values stay distinct from values withheld with receipts. The
observed `quokka-content-current-token` metadata value and matching
name/property-labeled page-context metadata content are withheld without
claiming their sensitivity; the original SDK capture and HTML input hashes are
also withheld because their inputs may contain the removed references. Nonsecret
fields and text, including Markdown labels and units, remain in the projected
representation. Receipts contain pointer/occurrence and projected-representation
bindings, not removed values or original-content hashes. Authenticate receipts
through the existing seal; standalone validation cannot establish an original
value existed. Projection does not reconstruct original URLs or source bytes.

Each observation part remains a separate collector artifact for conversion:

```sh
python3 -m source_evidence.cli convert-collector --artifact /private/part-0001.json --source-key leibsohn --collector-projection-version leibsohn-page-observation-withholding/v1 --out /private/converted.json
```

Only the witnessed scope and exact reference forms are projected. Ambiguous
encoded Google Maps references are refused. Other unrecognized strings remain
unchanged for the existing scanners, which check only their known credential
patterns and are not general secret detectors. This local
projection does not authorize source requests, database writes, canonical
listings, or production activation.

### Cawley Chicago and Bradford Allen page observations

The same bounded observation mechanics are separately opt-in for
`cawley-chicago-page-observation-withholding/v1` and
`bradford-allen-page-observation-withholding/v1`. Each version accepts only its
matching source key and observe-only collector parts. The original Leibsohn
version remains separately bound. The profiles retain noncredential sections,
card occurrences, labels, values, units and ordering; they project only the
witnessed map query and explicitly withhold the known token-named metadata and
original-body hash fields. The same numeric-witness, ambiguous-reference,
scanner and authenticated-receipt boundaries above apply. A profile is not
permission to fetch documents, promote contacts or write to production.
