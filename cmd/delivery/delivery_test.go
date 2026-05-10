package main

import "testing"

func TestShouldNotify(t *testing.T) {
	both := notifPrefs{notifyAtBat: true, notifyOnDeck: true}
	atBatOnly := notifPrefs{notifyAtBat: true, notifyOnDeck: false}
	onDeckOnly := notifPrefs{notifyAtBat: false, notifyOnDeck: true}
	neither := notifPrefs{notifyAtBat: false, notifyOnDeck: false}

	tests := []struct {
		name           string
		prefs          notifPrefs
		state          string
		hasPriorOnDeck bool
		want           bool
	}{
		// Both prefs enabled
		{"both / at_bat", both, "at_bat", false, true},
		{"both / on_deck", both, "on_deck", false, true},

		// at_bat only
		{"at_bat only / at_bat event", atBatOnly, "at_bat", false, true},
		{"at_bat only / on_deck event suppressed", atBatOnly, "on_deck", false, false},

		// on_deck only
		{"on_deck only / on_deck event", onDeckOnly, "on_deck", false, true},
		// Game-start and pinch-hitter: player goes straight to at_bat with no prior on_deck.
		{"on_deck only / at_bat / no prior on_deck (game start or pinch hit)", onDeckOnly, "at_bat", false, true},
		// Normal second PA: on_deck was already sent, so skip the at_bat.
		{"on_deck only / at_bat / prior on_deck exists", onDeckOnly, "at_bat", true, false},

		// Neither pref
		{"neither / at_bat", neither, "at_bat", false, false},
		{"neither / on_deck", neither, "on_deck", false, false},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := shouldNotify(tc.prefs, tc.state, tc.hasPriorOnDeck)
			if got != tc.want {
				t.Errorf("shouldNotify(%+v, %q, hasPrior=%v) = %v, want %v",
					tc.prefs, tc.state, tc.hasPriorOnDeck, got, tc.want)
			}
		})
	}
}
