# Days Counting Web App

## What is this project?
This is a simple web application that helps users track the number of days since a particular event. It also includes a journaling feature, allowing users to log entries and view them in a user-friendly interface. The app is built with Python and uses HTML templates for rendering web pages. It is designed to be deployed easily, including support for Vercel hosting.

## How does it work?
- **Backend:** The core logic is implemented in Python (`main.py`, `daysSince.py`, `ai_client.py`).
- **Frontend:** HTML templates are located in the `templates/` folder, with static assets in `static/`.
- **Journal Feature:** Users can write journal entries, which are stored and displayed in the app. The main journal file is `journal.txt`.
- **Days Counter:** The app calculates the number of days since a user-specified date or event.
- **Deployment:** The app can be deployed to Vercel using the provided `vercel.json` configuration.
- **Requirements:** All Python dependencies are listed in `requirements.txt`.

## Why was this project created?
- **Personal Tracking:** To help users keep track of important milestones or events in their lives.
- **Journaling:** To provide a simple, private space for users to reflect and record thoughts.
- **Learning:** To demonstrate how to build and deploy a Python-based web app with templating and basic file I/O.
- **Accessibility:** The app is lightweight, easy to set up, and can be hosted for free on platforms like Vercel.

## Getting Started
1. **Clone the repository:**
   ```powershell
   git clone https://github.com/bobabulker/days-counting-web-app-w-vercel.git
   cd days-counting-web-app-w-vercel
   ```
2. **Install dependencies:**
   ```powershell
   pip install -r requirements.txt
   ```
3. **Run the app locally:**
   ```powershell
   python main.py
   ```
4. **Access the app:**
   Open your browser and go to `http://localhost:5000` (or the port specified in your app).

## File Structure
- `main.py` - Main application entry point
- `daysSince.py` - Logic for calculating days since an event
- `ai_client.py` - (Optional) AI-related features
- `journal.txt` - Stores journal entries
- `requirements.txt` - Python dependencies
- `vercel.json` - Vercel deployment configuration
- `static/` - Static files (e.g., `index.html`)
- `templates/` - HTML templates for rendering pages

## Deployment
To deploy on Vercel:
1. Push your code to GitHub.
2. Connect your repository to Vercel.
3. Vercel will use `vercel.json` to configure the deployment.

## Contributing
Feel free to fork the repository and submit pull requests. Suggestions and improvements are welcome!

## License
This project is open source. See the repository for license details.

## Contact
For questions or feedback, open an issue on GitHub or contact the repository owner.
