package main

import (
	"fmt"
	"os"

	"github.com/theunknownport/aight-clients/go"
)

func callLLM() {
	aight.TracedLLMCall("gpt-4o-mini", 180, 60)
}

func main() {
	callLLM()
	callLLM()

	fmt.Println(aight.CostProcessor.Report())

	if os.Getenv("AIGHT_API_KEY") != "" {
		if err := aight.Push(aight.CostProcessor, "", ""); err != nil {
			fmt.Println("push failed:", err)
			return
		}
		fmt.Println("pushed to aight.studio")
	}
}
