fixme make this a proper guide for setting up Raspberry Pi with Tailscale and user-data modifications.

* install a virgin image on the sdcard with Raspberry Pi Imager appimage.
https://www.raspberrypi.com/software/ (this gui will guide setting wifi and network name etc...)

## temp login

ssh -o PubkeyAuthentication=no yyy@192.168.xxx

sudo raspi-config # boot/display/text console instead of gui


curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale set --ssh --operator=$USER

curl -fsSL https://ollama.com/install.sh | sh

github cli install
(type -p wget >/dev/null || (sudo apt update && sudo apt install wget -y)) \
	&& sudo mkdir -p -m 755 /etc/apt/keyrings \
	&& out=$(mktemp) && wget -nv -O$out https://cli.github.com/packages/githubcli-archive-keyring.gpg \
	&& cat $out | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null \
	&& sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
	&& sudo mkdir -p -m 755 /etc/apt/sources.list.d \
	&& echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
	&& sudo apt update \
	&& sudo apt install gh -y


ollama run hf.co/bartowski/Hermes-3-Llama-3.2-3B-GGUF # fast ehough to use - actually seems a little funnier than the bigger nemo
ollama ps
NAME                                                 ID              SIZE      PROCESSOR    CONTEXT    RUNNER  UNTIL              
hf.co/bartowski/Hermes-3-Llama-3.2-3B-GGUF:latest    83faf325141a    2.9 GB    100% CPU     4096       llamacpp  2 minutes from now    

# Mistral Nemo 12B (Q4 quantization) - pretty slow -  too slow
ollama run hf.co/bartowski/Mistral-Nemo-Instruct-2407-GGUF:Q4_K_M

# gemma4:e4b might be funny enough.  it is barely fast enough - only need 500mb ram
kevinh@voicebot:~/development/voicebot $ ollama run gemma4:e4b
pulling manifest 
pulling 370c2879f176: 100% ▕██████████████████████████████████████████████████ ▏ 5.5 GB/5.5 GB   24 MB/s      0s
pulling 4cb21b935cd7: 100% ▕██████████████████████████████████████████████████ ▏ 991 MB/991 MB   22 MB/s      0s
pulling 05612e54110f:  98% ▕██████████████████████████████████████████████████ ▏  97 MB/ 98 MB   27 MB/s      0s
pulling 7ca1ae564b24: 100% ▕███████████████████████████████████████████████████▏  241 B                         
verifying sha256 digest 
writing manifest 
success 
>>> can you say three funny things about turtles?
Here are three funny things about turtles:

1. **They are basically the ultimate slow-motion villains in nature.** Imagine a dramatic chase scene in a 
nature documentary, and the main antagonist is a tiny, armored turtle slowly, majestically shuffling across 
the frame, completely oblivious to the urgency. Its movement is the definition of "just taking it easy."
2. **Turtles are nature's most effective, low-maintenance portable bunker.** They don't just carry their 
house—they *are* their house. If a meteor were to strike, they'd be fine, provided the meteor wasn't also a 
giant fish. They're living, breathing, slow-moving, shell-encased fortresses.
3. **They have mastered the ancient art of the "Nope."** When things get uncomfortable, crowded, or 
generally stressful, the turtle does not engage. It simply retracts its head and limbs into its shell and 
commits to a lengthy period of profound, unshakeable indifference. It's the most effective "call time, I'm 
busy" move in the animal kingdom.

## ollama run qwen2.5:3b "can you say three funny things about turtles?"
very fast

Certainly! Here are three funny things about turtles:

1. Turtles never run away from their home - they just walk away from it!
2. The only way to make a turtle laugh is to give it a picture book of turtle jokes.
3. Turtles are so laid-back that when they go on a vacation, they just un-zip their shells and travel by 
sea!

from https://huggingface.co/models?num_parameters=min:3B,max:9B&sort=modified&search=creative
## ollama run hf.co/VertexAGI/prism-roleplay-2-small "can you say three funny things about turtles?"
too slow

## https://huggingface.co/mradermacher/Huihui-Qwen3-VL-8B-Instruct-Creative-v0.4-GGUF:

ollama run hf.co/mradermacher/Huihui-Qwen3-VL-8B-Instruct-Creative-v0.4-GGUF:Q4_K_S
a little slow but fast enough - default was not super funny

##  https://huggingface.co/mradermacher/gemma-3-4b-it-heretic-creative-GGUF

ollama run hf.co/mradermacher/gemma-3-4b-it-heretic-creative-GGUF:Q8_0 "can you say three funny things about turtles?"
too slow

ollama run hf.co/mradermacher/gemma-3-4b-it-heretic-creative-GGUF:Q4_K_M "can you say three funny things about turtles?"
fast enough not super funny


## todo
* make a good cloud init template per https://www.raspberrypi.com/news/cloud-init-on-raspberry-pi-os/
* install tailscale
* install ollama
* turn on fs overlay
* confirm can boot without network
* confirm all fs are read-only
* confirm connects to tailnet
* setup vscode remote development
* turn off non ts ssh
* turn on firewall to block all incoming except tailscale
* deauth github cli
* make installer script to deploy from working tree