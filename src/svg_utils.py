
def generate_shield_svg(label, status, color):
    # Estimated widths (Verdana 11px)
    # This is a rough approximation. For pixel-perfect, we'd need a font library, 
    # but that's overkill for MVP.
    char_width = 7
    label_w = len(label) * char_width + 10
    status_w = len(status) * char_width + 10
    total_w = label_w + status_w
    
    return f"""<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="{total_w}" height="20" role="img" aria-label="{label}: {status}">
    <title>{label}: {status}</title>
    <linearGradient id="s" x2="0" y2="100%"><stop offset="0" stop-color="#bbb" stop-opacity=".1"/><stop offset="1" stop-opacity=".1"/></linearGradient>
    <clipPath id="r"><rect width="{total_w}" height="20" rx="3" fill="#fff"/></clipPath>
    <g clip-path="url(#r)">
        <rect width="{label_w}" height="20" fill="#555"/>
        <rect x="{label_w}" width="{status_w}" height="20" fill="{color}"/>
        <rect width="{total_w}" height="20" fill="url(#s)"/>
    </g>
    <g fill="#fff" text-anchor="middle" font-family="Verdana,Geneva,DejaVu Sans,sans-serif" text-rendering="geometricPrecision" font-size="110">
        <text aria-hidden="true" x="{label_w * 5}" y="150" fill="#010101" fill-opacity=".3" transform="scale(.1)" textLength="{label_w * 10 - 100}">{label}</text>
        <text x="{label_w * 5}" y="140" transform="scale(.1)" fill="#fff" textLength="{label_w * 10 - 100}">{label}</text>
        <text aria-hidden="true" x="{(label_w + status_w/2) * 10}" y="150" fill="#010101" fill-opacity=".3" transform="scale(.1)" textLength="{status_w * 10 - 100}">{status}</text>
        <text x="{(label_w + status_w/2) * 10}" y="140" transform="scale(.1)" fill="#fff" textLength="{status_w * 10 - 100}">{status}</text>
    </g>
    </svg>"""
