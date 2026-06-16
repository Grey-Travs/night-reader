import { createContext, useContext } from 'react'

// Global on/off for the little "?" hint bubbles, so toggling it in the Guide hides
// every hint at once. App holds the state; Hint and GuidePanel read it here.
export const HintsContext = createContext({ on: true, setOn: () => {} })
export const useHints = () => useContext(HintsContext)
