export interface TabOverflow {
  left: boolean;
  right: boolean;
}

/** Which ends of the tab row have tabs scrolled out of view. */
export function tabOverflow(element: Pick<HTMLElement, 'scrollLeft' | 'scrollWidth' | 'clientWidth'>): TabOverflow {
  const hidden = element.scrollWidth - element.clientWidth;
  return { left: hidden > 1 && element.scrollLeft > 1, right: hidden > 1 && element.scrollLeft < hidden - 1 };
}
