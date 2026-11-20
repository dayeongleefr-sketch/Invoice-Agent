"""Browser-only input and scroll behavior; no verification or business decisions."""

ENTER_HANDLER = r'''e => {
    if (e.key !== 'Enter' || e.shiftKey || e.isComposing || e.keyCode === 229 ||
        e.target.dataset.composing === 'true') return;
    e.preventDefault();
    if (e.repeat || !e.target.value.trim()) return;
    window.skinChat?.follow();
    emit(e.target.value);
}'''

SCROLL_SCRIPT = r'''(() => {
    const list = document.querySelector('[data-testid=chat-messages]');
    const button = document.querySelector('[data-testid=latest-message]');
    if (!list || !button || window.skinChat?.list === list) return;
    window.skinChat?.dispose();
    let pinned = true, unread = false;
    const visible = () => list.getClientRects().length > 0;
    const nearBottom = () => document.documentElement.scrollHeight - innerHeight - scrollY < 180;
    const paint = () => { button.style.display = unread && !pinned && visible() ? 'inline-flex' : 'none'; };
    const latest = (smooth = true) => {
        pinned = true; unread = false; paint();
        requestAnimationFrame(() => window.scrollTo({top: document.documentElement.scrollHeight,
            behavior: smooth && !matchMedia('(prefers-reduced-motion: reduce)').matches ? 'smooth' : 'instant'}));
    };
    window.skinChat = {list, follow: () => {pinned = true; unread = false; paint();}, latest,
        open: () => setTimeout(() => latest(false), 60)};
    const onScroll = () => {pinned = nearBottom(); if (pinned) unread = false; paint();};
    window.addEventListener('scroll', onScroll, {passive:true});
    const observer = new MutationObserver(() => {
        if (!visible()) return;
        if (pinned) latest(); else {unread = true; paint();}
    });
    observer.observe(list, {childList:true, subtree:true, characterData:true});
    window.skinChat.dispose = () => {observer.disconnect(); window.removeEventListener('scroll', onScroll);};
    button.addEventListener('click', () => latest());
    paint();
})()'''
