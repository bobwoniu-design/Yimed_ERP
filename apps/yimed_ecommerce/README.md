### Yimed Ecommerce

Yimed channel e-commerce operations for JD procurement, packing, handover and stock transfer

### JD procurement MVP decisions

- Excel import only; no JD API integration.
- One JD purchase order creates at most one active Stock Entry.
- Different JD purchase orders cannot share a carton.
- A carton may contain multiple JD SKUs from the same purchase order.
- Carton-label SKU quantity means the number of distinct JD SKUs.
- Carton labels are 60 mm x 70 mm and contain no barcode or QR code.
- Product combinations reuse ERPNext Product Bundle. No duplicate JD bundle master is maintained.
- The Product Bundle parent is the business Item; physical components are expanded for packing and stock transfer.
- Batch dates come from ERPNext Batch. Remaining shelf-life ratios are recorded for reference and do not block JD packing.
- Equal single-SKU cartons and repeated mixed-SKU carton templates can be generated in bulk.
- Batch assignment, carton verification and 60 mm x 70 mm label printing are purchase-order-level bulk actions.
- The agreed legacy SKU workbook headers `链接主SKU` and `SKU` are accepted by the mapping importer.
- Stock Entry submission/cancellation updates the JD purchase order. ERPNext's native End Transit entry completes a two-step transfer.
- Stocking and warehouse summaries use A4 landscape print templates; the JD handover form remains a separate document.
- Company-level JD settings provide default source, target and optional transit warehouses while keeping server-side route validation.
- Purchase-order imports support the original JD export as well as a downloadable two-sheet template; failed batches can export error details.
- Packing and draft-transfer rework requires a reason, preserves an audit comment and never deletes a submitted Stock Entry.

### Inventory model

Ordinary platform Items move directly. When a mapped platform Item is an enabled Product Bundle, the JD
workflow expands its component quantities. Carton detail retains both the logical JD SKU and the physical
stock Item, while Stock Entry contains only physical stock Items.

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch version-16
bench install-app yimed_ecommerce
```

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/yimed_ecommerce
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade

### License

mit


### Procurement work sheets

The stocking, assembly and sorting stages provide Excel export and A4 landscape PDF printing through `jd.workbench_reports`. Stocking prints without batch numbers; current unsaved stocking/sorting inputs are rendered as a draft without changing business records. Sorting respects the selected purchase order. PDF generation uses the existing Frappe Chromium renderer, Noto CJK fonts and pypdf page numbering. On Ubuntu the headless browser needs `libnss3`, `libnspr4`, `libasound2t64` and `fonts-noto-cjk`; `poppler-utils` is used for visual QA.
