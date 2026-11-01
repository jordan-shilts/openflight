export type UnitSystem = 'imperial' | 'metric';

const MPH_TO_KMH = 1.60934;
const YARDS_TO_METERS = 0.9144;
const FEET_TO_METERS = 0.3048;
const IMPERIAL_SPEED_UNIT = 'mph';
const METRIC_SPEED_UNIT = 'km/h';
const IMPERIAL_DISTANCE_UNIT = 'yds';
const METRIC_DISTANCE_UNIT = 'm';
const IMPERIAL_HEIGHT_UNIT = 'ft';
const METRIC_HEIGHT_UNIT = 'm';
const IMPERIAL_ROLL_UNIT = 'ft';
const METRIC_ROLL_UNIT = 'm';

export function convertSpeedFromMph(speedMph: number, unitSystem: UnitSystem): number {
  if (unitSystem === 'metric') {
    return speedMph * MPH_TO_KMH;
  }

  return speedMph;
}

export function convertDistanceFromYards(distanceYards: number, unitSystem: UnitSystem): number {
  if (unitSystem === 'metric') {
    return distanceYards * YARDS_TO_METERS;
  }

  return distanceYards;
}

export function convertHeightFromFeet(heightFeet: number, unitSystem: UnitSystem): number {
  if (unitSystem === 'metric') {
    return heightFeet * FEET_TO_METERS;
  }

  return heightFeet;
}

export function formatSpeed(speedMph: number, unitSystem: UnitSystem, digits = 1): string {
  return convertSpeedFromMph(speedMph, unitSystem).toFixed(digits);
}

export function formatDistance(distanceYards: number, unitSystem: UnitSystem, digits = 0): string {
  return convertDistanceFromYards(distanceYards, unitSystem).toFixed(digits);
}

/** Putt roll: whole feet, or meters to a tenth since a meter is over three feet. */
export function formatRollDistance(distanceFeet: number, unitSystem: UnitSystem): string {
  return unitSystem === 'metric' ? (distanceFeet * FEET_TO_METERS).toFixed(1) : distanceFeet.toFixed(0);
}

export function getRollDistanceUnit(unitSystem: UnitSystem): string {
  return unitSystem === 'metric' ? METRIC_ROLL_UNIT : IMPERIAL_ROLL_UNIT;
}

export function getSpeedUnit(unitSystem: UnitSystem): string {
  return unitSystem === 'metric' ? METRIC_SPEED_UNIT : IMPERIAL_SPEED_UNIT;
}

export function getDistanceUnit(unitSystem: UnitSystem): string {
  return unitSystem === 'metric' ? METRIC_DISTANCE_UNIT : IMPERIAL_DISTANCE_UNIT;
}

export function getHeightUnit(unitSystem: UnitSystem): string {
  return unitSystem === 'metric' ? METRIC_HEIGHT_UNIT : IMPERIAL_HEIGHT_UNIT;
}

export function getUnitsLabel(unitSystem: UnitSystem): string {
  return `${getSpeedUnit(unitSystem)} / ${getDistanceUnit(unitSystem)}`;
}

export function formatCarryRange(carryRange: [number, number], unitSystem: UnitSystem): string {
  const min = formatDistance(carryRange[0], unitSystem, 0);
  const max = formatDistance(carryRange[1], unitSystem, 0);
  return `${min}-${max} ${getDistanceUnit(unitSystem)}`;
}
