/** What the Live body shows under its header: the metric table or the side-view flight. */
export type LiveView = 'metrics' | 'flight';

export function otherLiveView(view: LiveView): LiveView {
  return view === 'metrics' ? 'flight' : 'metrics';
}
