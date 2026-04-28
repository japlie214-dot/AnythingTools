// utils/javascript/frame_unmark_elements.js
// Remove temporary AI attributes and legacy badges
// Compatible with Botasaurus run_js method (synchronous execution)

(function(args) {
    const BID_ATTR = args.bid_attr || "data-ai-id";
    const VIS_ATTR = "browsergym_visibility_ratio";
    const SOM_ATTR = "browsergym_set_of_marks";

    const attrs = [BID_ATTR, VIS_ATTR, SOM_ATTR];
    document.querySelectorAll("*").forEach(function(el) {
        attrs.forEach(function(attr) {
            if (el.hasAttribute(attr)) {
                el.removeAttribute(attr);
            }
        });
    });

    // Remove legacy data-ai-badge overlays
    document.querySelectorAll('[data-ai-badge]').forEach(function(el) {
        el.remove();
    });

    return { cleaned: true };
})(arguments[0]);
