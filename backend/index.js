const express = require('express');
const bodyParser = require('body-parser');
const mongoose = require('mongoose');
const cors = require('cors');

const app = express();

app.use(cors({
  origin: '*', 
  methods: ['GET', 'POST', 'PUT', 'DELETE'],
  allowedHeaders: ['Content-Type']
}));

app.use(bodyParser.json());

const taskSchema = new mongoose.Schema({
  taskName: String,
  completed: Boolean
});

const Task = mongoose.model('Task', taskSchema);

async function connectToMongoDB() {
  const maxRetries = 10;
  const retryDelay = 5000; 

  for (let i = 0; i < maxRetries; i++) {
    try {
      console.log(`Attempting to connect to MongoDB (attempt ${i + 1}/${maxRetries})...`);
      
      await mongoose.connect(
        `mongodb://${process.env.MONGO_USER}:${process.env.MONGO_PASS}@mongo-service:27017/taskdb?authSource=admin`
      );
      
      console.log('Connected to MongoDB successfully');
      return true;
    } catch (error) {
      console.error(`Error connecting to MongoDB (attempt ${i + 1}/${maxRetries}):`, error.message);
      
      if (i < maxRetries - 1) {
        console.log(`Retrying in ${retryDelay / 1000} seconds...`);
        await new Promise(resolve => setTimeout(resolve, retryDelay));
      } else {
        console.error('Failed to connect to MongoDB after all retries');
        process.exit(1);
      }
    }
  }
}

// Routes

// Health check endpoint
app.get('/health', (req, res) => {
  const status = mongoose.connection.readyState === 1 ? 'healthy' : 'unhealthy';
  const statusCode = mongoose.connection.readyState === 1 ? 200 : 503;
  res.status(statusCode).json({ 
    status,
    mongodb: mongoose.connection.readyState === 1 ? 'connected' : 'disconnected'
  });
});

// GET /tasks - Retrieve all tasks
app.get('/tasks', async (req, res) => {
  try {
    const tasks = await Task.find();
    console.log(`Fetched ${tasks.length} tasks from the database.`);
    res.json(tasks);
  } catch (error) {
    console.error('Error fetching tasks:', error);
    res.status(500).json({ error: 'Error fetching tasks' });
  }
});

// POST /tasks - Create a new task
app.post('/tasks', async (req, res) => {
  const { taskName } = req.body;
  console.log(`Attempting to insert task: ${taskName}`);

  const newTask = new Task({
    taskName,
    completed: false
  });

  try {
    const savedTask = await newTask.save();
    console.log(`Task inserted successfully: ${savedTask}`);
    res.status(201).json(savedTask);
  } catch (error) {
    console.error('Error inserting task:', error);
    res.status(500).json({ error: 'Error inserting task' });
  }
});

const PORT = 3000;


connectToMongoDB().then(() => {
  app.listen(PORT, () => {
    console.log(`🚀 Server running on port ${PORT}`);
  });
}).catch((error) => {
  console.error('Failed to start server:', error);
  process.exit(1);
});