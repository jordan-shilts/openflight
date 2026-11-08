import { Fragment, type HTMLAttributes, type ReactNode, type Ref } from 'react';
import './TabBar.css';

/** Extra attributes for the <nav>, e.g. a ref and drag-scroll handlers when it overflows. */
export type TabBarNavProps = Omit<HTMLAttributes<HTMLElement>, 'className' | 'aria-label'> & {
  ref?: Ref<HTMLElement>;
};

export function TabBar<T extends string>({
  value,
  options,
  onChange,
  className,
  ariaLabel,
  separator,
  navProps,
}: {
  value: T;
  options: ReadonlyArray<{
    id: T;
    label: string;
    icon?: ReactNode;
    badge?: ReactNode;
    extraClassName?: string;
  }>;
  onChange: (id: T) => void;
  /** Extra class on the <nav>, so a host layout can restyle the bar in place. */
  className?: string;
  ariaLabel?: string;
  /** Hairline (or other) separator rendered between items. */
  separator?: ReactNode;
  navProps?: TabBarNavProps;
}) {
  return (
    <nav {...navProps} className={className ? `nav ${className}` : 'nav'} aria-label={ariaLabel}>
      {options.map((option, index) => {
        const active = option.id === value;
        const classes = ['nav__button'];
        if (active) {
          classes.push('nav__button--active');
        }
        if (option.extraClassName) {
          classes.push(option.extraClassName);
        }

        return (
          <Fragment key={option.id}>
            {index > 0 ? separator : null}
            <button
              type="button"
              className={classes.join(' ')}
              aria-pressed={active}
              onClick={() => onChange(option.id)}
            >
              {option.icon ? option.icon : null}
              <span>{option.label}</span>
              {option.badge ? option.badge : null}
            </button>
          </Fragment>
        );
      })}
    </nav>
  );
}
