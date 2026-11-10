import { describe, expect, it } from 'vitest';
import {
  clubSections,
  initialPickerSection,
  pickerGridRows,
  resolvePickerSection,
  trainingImplementSections,
} from './pickerSections';

describe('initialPickerSection', () => {
  const clubs = clubSections();

  it('opens Woods when the driver is selected', () => {
    expect(initialPickerSection(clubs, 'driver')).toBe('Woods');
  });

  it('opens Irons when a wedge is selected', () => {
    expect(initialPickerSection(clubs, 'pw')).toBe('Irons');
  });

  it('opens Putter when the putter is selected', () => {
    expect(initialPickerSection(clubs, 'putter')).toBe('Putter');
  });

  it('falls back to the first section when the id is unknown', () => {
    expect(initialPickerSection(clubs, 'not-a-club')).toBe('Irons');
  });

  it('opens the matching training group', () => {
    const groups = trainingImplementSections();
    expect(initialPickerSection(groups, 'stack-160g')).toBe('TheStack');
  });
});

describe('resolvePickerSection', () => {
  const clubs = clubSections();

  it('follows the selection until a tab is tapped', () => {
    // At launch the picker opens on the default, then the saved club arrives.
    expect(resolvePickerSection(clubs, 'driver', null)).toBe('Woods');
    expect(resolvePickerSection(clubs, '7-iron', null)).toBe('Irons');
  });

  it('stays on the tab the user tapped, whatever is selected', () => {
    expect(resolvePickerSection(clubs, '7-iron', 'Woods')).toBe('Woods');
    expect(resolvePickerSection(clubs, 'driver', 'Putter')).toBe('Putter');
  });

  it('ignores a tapped tab this picker does not have', () => {
    // Clubs and training implements share the overlay; their tabs differ.
    expect(resolvePickerSection(clubs, 'pw', 'TheStack')).toBe('Irons');
  });
});

describe('pickerGridRows', () => {
  it('uses three rows for the iron set so woods tiles match that cube size', () => {
    expect(pickerGridRows(clubSections())).toBe(3);
  });

  it('uses the densest training group so every tab fits', () => {
    expect(pickerGridRows(trainingImplementSections())).toBe(4);
  });
});
