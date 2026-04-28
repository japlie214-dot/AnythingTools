// utils/javascript/frame_mark_elements.js
// Set-of-Marks injection with flat bid namespace and interactive heuristics
// Compatible with Botasaurus run_js method (synchronous execution)

(function(args) {
    const BID_ATTR = args.bid_attr || "data-ai-id";
    const VIS_ATTR = "browsergym_visibility_ratio";
    const SOM_ATTR = "browsergym_set_of_marks";
    const TAGS_TO_MARK = args.tags_to_mark || "standard_html";

    const html_tags = new Set([
        "a","abbr","acronym","address","applet","area","article","aside","audio",
        "b","base","basefont","bdi","bdo","big","blockquote","body","br","button",
        "canvas","caption","center","cite","code","col","colgroup","data","datalist",
        "dd","del","details","dfn","dialog","dir","div","dl","dt","em","embed",
        "fieldset","figcaption","figure","font","footer","form","frame","frameset",
        "h1","h2","h3","h4","h5","h6","head","header","hgroup","hr","html","i",
        "iframe","img","input","ins","kbd","label","legend","li","link","main",
        "map","mark","menu","meta","meter","nav","noframes","noscript","object",
        "ol","optgroup","option","output","p","param","picture","pre","progress",
        "q","rp","rt","ruby","s","samp","script","search","section","select",
        "small","source","span","strike","strong","style","sub","summary","sup",
        "svg","table","tbody","td","template","textarea","tfoot","th","thead",
        "time","title","tr","track","tt","u","ul","var","video","wbr"
    ]);

    const set_of_marks_tags = new Set([
        "input","textarea","select","button","a","iframe","video","li","td","option"
    ]);

    function isVisible(elem) {
        const rect = elem.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) return false;
        const style = window.getComputedStyle(elem);
        if (style.visibility === "hidden" || style.display === "none" || style.opacity === "0") return false;
        return true;
    }

    // Collect all elements including shadow DOM
    let elements = Array.from(document.querySelectorAll("*"));
    let i = 0;
    while (i < elements.length) {
        const elem = elements[i];
        if (elem.shadowRoot !== null) {
            elements = elements.slice(0, i + 1)
                .concat(Array.from(elem.shadowRoot.querySelectorAll("*")))
                .concat(elements.slice(i + 1));
        }
        i++;
    }

    let bidCounter = 0;
    let allBids = new Set();
    let somButtons = [];
    let markedCount = 0;
    let somCount = 0;

    for (const elem of elements) {
        if (TAGS_TO_MARK === "standard_html") {
            if (!elem.tagName || !html_tags.has(elem.tagName.toLowerCase())) continue;
        }

        const visible = isVisible(elem);
        elem.setAttribute(VIS_ATTR, visible ? "1" : "0");
        if (!visible) continue;

        // Serialize dynamic attributes
        if (typeof elem.value !== "undefined") {
            elem.setAttribute("value", elem.value);
        }
        if (typeof elem.checked !== "undefined") {
            if (elem.checked) elem.setAttribute("checked", "");
            else elem.removeAttribute("checked");
        }

        // Inject flat bid (data-ai-id)
        let bid = null;
        if (elem.hasAttribute(BID_ATTR)) {
            bid = elem.getAttribute(BID_ATTR);
            if (allBids.has(bid)) {
                bid = null;
            }
        }
        if (bid === null) {
            bid = "bid_" + (bidCounter++);
            elem.setAttribute(BID_ATTR, bid);
        }
        allBids.add(bid);
        markedCount++;

        // Set SoM marker
        elem.setAttribute(SOM_ATTR, "0");
        const tag = elem.tagName.toLowerCase();
        const hasClickHandler = (elem.onclick != null);
        const isPointer = (window.getComputedStyle(elem).cursor === "pointer");
        
        if (set_of_marks_tags.has(tag) || hasClickHandler || isPointer) {
            const rect = elem.getBoundingClientRect();
            const area = rect.width * rect.height;
            if (area >= 20) {
                const notContained = somButtons.every(btn => !btn.contains(elem));
                const parent = elem.parentElement;
                const notSoleSpanChild = !(
                    parent && parent.tagName.toLowerCase() === "span" &&
                    parent.children.length === 1 &&
                    parent.getAttribute("role") &&
                    parent.getAttribute(SOM_ATTR) === "1"
                );
                if (notContained && notSoleSpanChild) {
                    elem.setAttribute(SOM_ATTR, "1");
                    if (elem.matches('button, a, input[type="button"], div[role="button"]')) {
                        somButtons.push(elem);
                    }
                    // Remove SoM from parent if this is the interactive element
                    let p = parent;
                    while (p) {
                        if (p.getAttribute(SOM_ATTR) === "1") {
                            p.setAttribute(SOM_ATTR, "0");
                        }
                        p = p.parentElement;
                    }
                    somCount++;
                }
            }
        }
    }

    return {
        marked_count: markedCount,
        som_count: somCount,
        last_bid: bidCounter > 0 ? "bid_" + (bidCounter - 1) : null
    };
})(arguments[0]);
